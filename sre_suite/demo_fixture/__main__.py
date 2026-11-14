"""Offline private preparation and explicit local controller/export startup."""

import argparse
import os
from pathlib import Path

from ..api_wire import fields
from ..execution.journal import Journal
from ..execution.wire import canonical
from ..fixtures.contracts import FixturePolicy, FixtureRequest
from ..telemetry.schedule_config import private_path
from ..verification.__main__ import write_new
from ..verification.config import client, private_document
from .controller import Controller, recipe, validate_target
from .export import flush, transports
from .scenarios import matrix, prepare_policy
from .server import server


def target_document(path: Path) -> tuple[FixturePolicy, object, object]:
    raw = fields(
        private_document(path, 1_048_576), {"schema_version", "policy", "request", "recipe"}
    )
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 2:
        raise ValueError("unsupported target schema")
    policy = FixturePolicy.parse(raw["policy"])
    validate_target(policy, FixtureRequest.parse(raw["request"]), raw["recipe"])
    return policy, raw["request"], raw["recipe"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Pinned demo fixture controller; no deployment")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("matrix", "expectations-template", "recipe", "prepare", "pin"):
        p = sub.add_parser(command)
        p.add_argument("--output", type=Path, required=True)
        if command == "recipe":
            p.add_argument("--repository", required=True)
            p.add_argument("--revision", required=True)
        if command == "prepare":
            p.add_argument("--verification-policy", type=Path, required=True)
            p.add_argument("--expectations", type=Path, required=True)
            p.add_argument("--target-origin", required=True)
            p.add_argument("--loopback-ca", action="store_true")
            p.add_argument("--runner", required=True)
        if command == "pin":
            for name in ("policy", "request", "recipe"):
                p.add_argument("--" + name, type=Path, required=True)
    for command in ("validate", "serve", "export"):
        p = sub.add_parser(command)
        p.add_argument("--target", type=Path, required=True)
        if command != "validate":
            p.add_argument("--state", type=Path, required=True)
        if command == "serve":
            p.add_argument("--cert", type=Path, required=True)
            p.add_argument("--key", type=Path, required=True)
            p.add_argument("--port", type=int, default=9443)
            p.add_argument("--authorize-fixtures", action="store_true")
        if command == "export":
            p.add_argument("--export-config", type=Path, required=True)
            p.add_argument("--authorize-export", action="store_true")
    args = parser.parse_args()
    try:
        if args.command in {"serve", "export"} and not getattr(
            args, "authorize_" + ("fixtures" if args.command == "serve" else "export")
        ):
            raise ValueError("explicit operation authorization is required")
        if args.command == "matrix":
            write_new(args.output, {"schema_version": 2, "checks": matrix()})
        elif args.command == "expectations-template":
            write_new(
                args.output, {k: None for k, v in matrix().items() if v["expectation_required"]}
            )
        elif args.command == "recipe":
            write_new(args.output, recipe(args.repository, args.revision))
        elif args.command == "prepare":
            write_new(
                args.output,
                prepare_policy(
                    private_document(args.verification_policy, 1_048_576),
                    private_document(args.expectations),
                    {
                        "base_url": args.target_origin,
                        "authorization_env": "SRE_FIXTURE_TARGET_AUTH",
                        "ca_env": "SRE_FIXTURE_TARGET_CA_FILE" if args.loopback_ca else None,
                    },
                    args.runner,
                ),
            )
        elif args.command == "pin":
            policy = private_document(args.policy, 1_048_576)
            request = private_document(args.request)
            source = private_document(args.recipe)
            validate_target(FixturePolicy.parse(policy), FixtureRequest.parse(request), source)
            write_new(
                args.output,
                {"schema_version": 2, "policy": policy, "request": request, "recipe": source},
            )
        else:
            policy, request, source = target_document(args.target)
            if args.command == "validate":
                print(canonical({"valid": True, "dispatch": policy.dispatch, "checks": 120}))
                return 0
            controller = Controller(policy, request, source, args.state, client(os.environ))
            if args.command == "serve":
                for path in (args.cert, args.key):
                    private_path(path, directory=False)
                with server(
                    controller,
                    os.environ["SRE_DEMO_FIXTURE_AUTH"],
                    os.environ["SRE_DEMO_METRICS_AUTH"],
                    str(args.cert),
                    str(args.key),
                    args.port,
                ) as listener:
                    listener.serve_forever(poll_interval=0.25)
            else:
                clients = transports(private_document(args.export_config), os.environ)
                count = flush(controller, clients, Journal(args.state / "export-journal"))
                print(canonical({"delivered_batches": count}))
                return 0
        print(canonical({"prepared": True, "started": False}))
        return 0
    except Exception:
        print(
            "Demo fixture operation blocked; inspect private inputs and scoped service readiness."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
