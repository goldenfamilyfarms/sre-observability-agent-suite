"""Private build-worker configuration; registry credentials never enter the build process."""

import os
import re
from pathlib import Path

from ..api_wire import fields, text
from ..control.client import ControlClient
from ..execution.http import JsonHttp
from ..execution.journal import Journal
from ..execution.wire import digest
from ..telemetry.schedule_config import private_path
from ..verification.config import client, private_document
from .contracts import BuildRequest
from .providers import Tools
from .runtime import BuildRuntime


def load_runtime(path: Path) -> BuildRuntime:
    config = dict(
        fields(
            private_document(path),
            {
                "request",
                "roles",
                "producer",
                "repository",
                "journal",
                "tools",
            },
        )
    )
    request_path = Path(text(config["request"]))
    document = private_document(request_path)
    request = BuildRequest.parse(document)
    pinned = digest([config, document])
    roles = {
        k: text(v, 128)
        for k, v in fields(config["roles"], {"implement", "review", "test", "qa"}).items()
    }
    for subject in [config["producer"], *roles.values()]:
        text(subject, 128)
    tool_config = dict(
        fields(
            config["tools"],
            {
                "buildx",
                "skopeo",
                "docker_config",
                "docker_context",
                "builder",
                "registry_auth_file",
                "registry_ca",
            },
        )
    )
    for key in ("buildx", "skopeo"):
        binary = fields(tool_config[key], {"path", "sha256"})
        if not re.fullmatch(r"[0-9a-f]{64}", text(binary["sha256"])):
            raise ValueError("tool binary SHA256 required")
    for key in ("docker_context", "builder"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", text(tool_config[key])):
            raise ValueError("explicit Docker context and builder required")

    def guard() -> None:
        if digest([private_document(path), private_document(request_path)]) != pinned:
            raise ValueError("build configuration changed")

    private_path(Path(text(config["journal"])), directory=True)
    if any(c in text(config["journal"]) for c in ",:\n\r"):
        raise ValueError("journal path cannot contain image transport separators")

    return BuildRuntime(
        request,
        roles,
        text(config["producer"]),
        Path(text(config["repository"])),
        client(os.environ),
        ControlClient(
            JsonHttp(
                os.environ["SRE_CONTROL_URL"],
                "Bearer " + os.environ["SRE_CONTROL_TOKEN"],
                local_ca_file=os.environ.get("SRE_CONTROL_CA_FILE"),
            )
        ),
        Journal(Path(text(config["journal"]))),
        Tools(tool_config),
        policy_guard=guard,
    )
