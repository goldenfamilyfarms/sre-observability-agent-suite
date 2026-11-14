"""Explicit private control requests and loopback HTTPS host; no startup on import."""

import argparse
import json
import os
from pathlib import Path
from wsgiref.types import StartResponse, WSGIEnvironment

from ..api_wire import fields, text
from ..auth import TokenAuthenticator
from ..execution.http import JsonHttp
from ..execution.wire import canonical
from ..local_pilot.config import AUDIENCE, load_credentials
from ..local_pilot.server import LocalHttpsServer, with_health
from ..serialization import scope_from_dict
from ..verification.config import private_document
from .api import OPERATIONS, ControlApi, ControlPolicy
from .client import ControlClient
from .contracts import identifier
from .store import prepare_sqlite


def application(config: Path) -> ControlApi:
    raw = fields(
        private_document(config),
        {
            "credentials",
            "humans",
            "stewards",
            "single_valued_predicates",
            "automatic_bindings",
            "database",
            "production_enabled",
        },
    )
    database = fields(raw["database"], {"kind", "location", "schema"})
    if database["kind"] == "sqlite":
        location = Path(text(database["location"]))
        from ..telemetry.schedule_config import private_path

        private_path(location.parent, directory=True)
        if location.exists() or location.is_symlink():
            private_path(location, directory=False)
        backend = prepare_sqlite(str(location))
        location.chmod(0o600)
    elif database["kind"] == "postgres":
        from ..api_storage import postgres_backend

        backend = postgres_backend(os.environ[text(database["location"])], text(database["schema"]))
    else:
        raise ValueError("unsupported database")
    humans: list[str] = []
    stewards: list[str] = []
    predicates: list[str] = []
    for key, destination in (
        ("humans", humans),
        ("stewards", stewards),
        ("single_valued_predicates", predicates),
    ):
        values = raw[key]
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError("bounded policy list required")
        destination.extend(identifier(v) for v in values)
    automatic = raw["automatic_bindings"]
    if not isinstance(automatic, list) or len(automatic) > 100:
        raise ValueError("bounded automatic authorizations required")
    pairs = []
    for item in automatic:
        row = fields(item, {"subject", "binding_sha256"})
        pairs.append((identifier(row["subject"]), text(row["binding_sha256"], 64)))
    credentials = Path(text(raw["credentials"]))
    private_document(credentials)
    if type(raw["production_enabled"]) is not bool:
        raise ValueError("explicit production enable flag required")
    auth = TokenAuthenticator(
        AUDIENCE, load_credentials(credentials, allow_production=bool(raw["production_enabled"]))
    )
    return ControlApi(
        auth,
        backend,
        policy=ControlPolicy(
            frozenset(humans),
            frozenset(stewards),
            frozenset(predicates),
            tuple(pairs),
            bool(raw["production_enabled"]),
        ),
        allow_preview_registration=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Authenticated suite control service")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--config", type=Path, required=True)
    serve.add_argument("--certificate", type=Path, required=True)
    serve.add_argument("--key", type=Path, required=True)
    serve.add_argument("--port", type=int, required=True)
    call = sub.add_parser("request")
    call.add_argument("operation", choices=sorted(OPERATIONS))
    call.add_argument("--request", type=Path, required=True)
    call.add_argument("--output", type=Path, required=True)
    steward = sub.add_parser("steward")
    steward.add_argument("--request", type=Path, required=True)
    steward.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "serve":
            from ..telemetry.schedule_config import private_path

            private_path(args.certificate, directory=False)
            private_path(args.key, directory=False)
            if not 1024 <= args.port <= 65535:
                raise ValueError("unprivileged port required")

            # Reload policy and credentials on each request, including revocations.
            def app(env: WSGIEnvironment, start: StartResponse) -> list[bytes]:
                return application(args.config)(env, start)

            with LocalHttpsServer(
                args.port,
                str(args.certificate),
                str(args.key),
                with_health(
                    app, lambda: application(args.config)._database().close(), lambda: None
                ),
            ) as server:
                server.serve_forever(poll_interval=0.2)
        elif args.command == "steward":
            from ..pilot_demo.prepare import publish_directory
            from ..verification.config import client as evidence_client
            from .steward import KnowledgeSteward, StewardRules

            raw = fields(
                private_document(args.request), {"scope", "producers", "predicates", "maximum"}
            )
            if not isinstance(raw["producers"], list) or not isinstance(raw["predicates"], list):
                raise ValueError("explicit producer and predicate lists required")
            from ..telemetry.contracts import integer

            rules = StewardRules(
                frozenset(identifier(p) for p in raw["producers"]),
                frozenset(identifier(p) for p in raw["predicates"]),
                integer(raw["maximum"], 1, 100),
            )
            control = ControlClient(
                JsonHttp(
                    os.environ["SRE_CONTROL_URL"],
                    "Bearer " + os.environ["SRE_CONTROL_TOKEN"],
                    local_ca_file=os.environ.get("SRE_CONTROL_CA_FILE"),
                )
            )
            result = KnowledgeSteward(control, evidence_client(os.environ), rules).run(
                scope_from_dict(raw["scope"])
            )
            publish_directory(args.output, {"result.json": (canonical(result) + "\n").encode()})
            print(json.dumps({"prepared": True}))
        else:
            from ..pilot_demo.prepare import publish_directory

            raw = fields(private_document(args.request), {"scope", "payload", "operation_id"})
            if not isinstance(raw["payload"], dict):
                raise ValueError("payload required")
            client = ControlClient(
                JsonHttp(
                    os.environ["SRE_CONTROL_URL"],
                    "Bearer " + os.environ["SRE_CONTROL_TOKEN"],
                    local_ca_file=os.environ.get("SRE_CONTROL_CA_FILE"),
                )
            )
            result = client.call(
                args.operation,
                scope_from_dict(raw["scope"]),
                raw["payload"],
                identifier(raw["operation_id"]),
            )
            publish_directory(args.output, {"result.json": (canonical(result) + "\n").encode()})
            print(json.dumps({"prepared": True}))
        return 0
    except Exception:
        print(json.dumps({"outcome": "blocked", "reason": "control_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
