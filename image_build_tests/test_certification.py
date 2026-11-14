"""Actual built image -> HTTPS fixtures -> real backends -> authenticated 120-slot report.

GKE/Argo responses are modeled: this test does not deploy to a Kubernetes cluster.
"""

import copy
import http.client
import json
import platform
import socket
import ssl
import threading
import time
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from image_build_tests import test_pipeline
from image_build_tests.reviewed_policy import reviewed
from sre_suite.api_wire import evidence_from_wire
from sre_suite.auth import Action, TokenAuthenticator
from sre_suite.delivery.contracts import DeliveryRequest
from sre_suite.delivery.helm import resources
from sre_suite.execution.http import JsonHttp, RemoteError
from sre_suite.execution.journal import Journal
from sre_suite.execution.wire import canonical
from sre_suite.fixtures.contracts import FixturePolicy, FixtureRequest
from sre_suite.fixtures.journeys import JourneyTarget
from sre_suite.fixtures.runtime import FixtureRuntime
from sre_suite.fixtures.target_service import verify
from sre_suite.local_pilot.config import write_json, write_private
from sre_suite.local_telemetry.config import DEFAULT_PORTS, initialize
from sre_suite.local_telemetry.lifecycle import running
from sre_suite.previews import PreviewRegistry
from sre_suite.target_service.prepare import prepare
from sre_suite.telemetry.adapters import transport
from sre_suite.telemetry.collector import TelemetryCollector
from sre_suite.telemetry.contracts import QueryPolicy
from sre_suite.verification.contracts import VerificationPolicy
from sre_suite.verification.runtime import CertificationEngine
from tests.test_api import AUDIENCE, credential
from tests.test_reference_deployment import reference_request


def free_ports():
    sockets = [socket.socket() for _ in DEFAULT_PORTS]
    try:
        for s in sockets:
            s.bind(("127.0.0.1", 0))
        return dict(zip(DEFAULT_PORTS, [s.getsockname()[1] for s in sockets], strict=True))
    finally:
        for s in sockets:
            s.close()


class ImageCertificationTests(test_pipeline.ImagePipelineTests):
    def setUp(self):
        now = datetime.now(UTC)
        for name in (
            "tests.image_build_fixture",
            "tests.delivery_fixture",
            "tests.execution_fixture",
            "tests.test_api",
        ):
            p = patch(name + ".NOW", now)
            p.start()
            self.addCleanup(p.stop)

    def certify(self, root, f, built, receipt, docker_call, image, cert, key):
        # Full mode requires the registry-verified receipt. Never forge a build attestation.
        self.assertIsNotNone(
            receipt, "Full certification requires Skopeo; build-only mode cannot certify"
        )
        d = f.delivery
        d.app._clock = lambda: datetime.now(UTC)
        d.runtime.clock = lambda: datetime.now(UTC)
        for subject in ("runner", "assessor", "collector", "verifier"):
            d.tokens[subject] = "synthetic-" + subject + "x" * 43
        d.app._auth = TokenAuthenticator(
            AUDIENCE,
            tuple(
                credential(d.scope, tuple(Action), token=token, subject=subject)
                for subject, token in d.tokens.items()
            ),
        )
        raw = reference_request(d.raw)
        raw.update(build=receipt["build"], stages=receipt["stages"])
        raw["binding"]["artifact_digest"] = built["image_digest"]
        raw["binding"]["target"].update(
            image=receipt["image"],
            candidate_revision=f.raw["binding"]["target"]["candidate_revision"],
        )
        d.runtime.request = request = DeliveryRequest.parse(raw)
        d.approve(request)
        d.runtime.deploy()  # In-memory GKE/Argo adapter, no cluster call.
        d.cluster.healthy(request)
        routes = {
            "Service": "/api/v1/namespaces/{}/services/",
            "PersistentVolumeClaim": "/api/v1/namespaces/{}/persistentvolumeclaims/",
            "Ingress": "/apis/networking.k8s.io/v1/namespaces/{}/ingresses/",
            "ServiceMonitor": "/apis/monitoring.coreos.com/v1/namespaces/{}/servicemonitors/",
        }
        for obj in resources(request)[1:]:
            obj = copy.deepcopy(obj)
            obj["metadata"].update(uid="modeled-resource", resourceVersion="1")
            obj["status"] = {"phase": "Bound"}
            d.cluster.objects[
                routes[obj["kind"]].format(request.target.raw["namespace"])
                + obj["metadata"]["name"]
            ] = obj
        proof = dict(
            build=receipt["build"],
            readiness=d.runtime.readiness()["evidence"],
            builder="builder",
            delivery="delivery",
            maximum_age_seconds=300,
        )
        release = dict(
            scope=asdict(d.scope),
            artifact_digest=built["image_digest"],
            plan_digest=request.binding.plan_digest,
            required_gates=["certification"],
        )
        identity = verify(
            proof,
            {"release": release, "artifact_kind": "container-image-v1", "context_version": 0},
            d.evidence,
            datetime.now(UTC),
        )
        owner = self
        self.address = None
        self.backends = {}

        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                path = self.path
                if self.command == "POST" and path in owner.backends:
                    url, ca = owner.backends[path]
                    origin = urlsplit(url)
                    connection = http.client.HTTPSConnection(
                        origin.hostname,
                        origin.port,
                        timeout=5,
                        context=ssl.create_default_context(cafile=str(ca)),
                    )
                elif (
                    self.command == "GET"
                    and path
                    in {"/ready", "/sre/identity", "/metrics", "/health", "/unicode", "/error"}
                    and owner.address
                ):
                    connection = http.client.HTTPConnection(owner.address, timeout=5)
                else:
                    self.send_error(503)
                    return
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= n <= 1_000_000:
                        self.send_error(413)
                        return
                    body = self.rfile.read(n) if n else None
                    headers = {
                        k: v
                        for k, v in self.headers.items()
                        if k.lower() not in {"host", "connection"}
                    }
                    connection.request(self.command, path, body=body, headers=headers)
                    reply = connection.getresponse()
                    data = reply.read(1_000_001)
                    self.send_response(reply.status)
                    self.send_header(
                        "Content-Type", reply.getheader("Content-Type", "application/json")
                    )
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (OSError, http.client.HTTPException):
                    self.send_error(503)
                finally:
                    connection.close()

        gateway = ThreadingHTTPServer(("0.0.0.0", 0), Gateway)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        gateway.socket = tls.wrap_socket(gateway.socket, server_side=True)
        thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (gateway.shutdown(), gateway.server_close(), thread.join()))
        origin = "https://localhost:" + str(gateway.server_port)
        now = datetime.now(UTC)
        manifest = root / "pilot.json"
        write_json(
            manifest,
            dict(
                schema_version=2,
                registration=dict(
                    schema_version=2, preview=asdict(d.f.preview), expected_generation=0
                ),
                ticket_key="DEMO-1",
                run_id="image-certification",
                baseline=dict(
                    sha256="d" * 64,
                    observed_at=now.isoformat(),
                    expires_at=(now + timedelta(minutes=30)).isoformat(),
                ),
            ),
        )
        metrics_token = root / "metrics.token"
        write_private(metrics_token, ("Bearer " + "m" * 40).encode())
        stack = root / "telemetry"
        config = initialize(
            stack,
            manifest,
            origin,
            cert,
            metrics_token,
            mode="native" if platform.system() == "Linux" else "desktop",
            ports=free_ports(),
        )
        for _signal, backend, path in (
            ("traces", "tempo", "/v1/traces"),
            ("logs", "loki", "/loki/api/v1/push"),
        ):
            self.backends[path] = (config.url(backend), stack / "server.crt")
        with running(stack, config, authorized=True):
            tokens = json.loads((stack / "tokens.json").read_text())
            # A dedicated named volume proves persistence across actual container replacement.
            volume = "sre-reference-state-" + uuid4().hex[:12]
            docker_call("volume", "create", volume)
            self.addCleanup(lambda: docker_call("volume", "rm", volume))
            public_ca = root / "container-ca.crt"
            public_ca.write_bytes(cert.read_bytes())
            public_ca.chmod(0o644)
            # Initialize ownership using the same verified image; no downloaded helper image.
            docker_call(
                "run",
                "--rm",
                "--network=none",
                "--user=0:0",
                "--entrypoint=python",
                "--mount",
                f"type=volume,source={volume},target=/state",
                image,
                "-c",
                "import os; os.chown('/state',65532,65532); os.chmod('/state',0o700)",
            )
            container = "sre-image-cert-" + uuid4().hex[:12]

            def remove():
                if (
                    container
                    in docker_call("ps", "-a", "--format", "{{.Names}}").decode().splitlines()
                ):
                    docker_call("rm", "--force", container)

            self.addCleanup(remove)
            export_origin = "https://host.docker.internal:" + str(gateway.server_port)
            env = {
                "OTEL_RESOURCE_ATTRIBUTES": ",".join(k + "=" + v for k, v in identity.items()),
                "SRE_TARGET_TOKEN": "s" * 40,
                "SRE_TARGET_METRICS_TOKEN": "m" * 40,
                "SRE_TARGET_STATE": "/state/reference.db",
                "SRE_TARGET_EXPORT_CA_FILE": "/trust/ca.crt",
                "SRE_TARGET_TRACES_URL": export_origin + "/v1/traces",
                "SRE_TARGET_LOGS_URL": export_origin + "/loki/api/v1/push",
                "SRE_TARGET_TRACES_AUTH": tokens["traces_write"],
                "SRE_TARGET_LOGS_AUTH": tokens["logs_write"],
            }

            def start():
                args = [
                    "run",
                    "--detach",
                    "--rm",
                    "--name",
                    container,
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--publish",
                    "127.0.0.1::8080",
                    "--mount",
                    f"type=volume,source={volume},target=/state",
                    "--mount",
                    f"type=bind,source={public_ca},target=/trust/ca.crt,readonly",
                ]
                if platform.system() == "Linux":
                    args += ["--add-host", "host.docker.internal:host-gateway"]
                for k, v in env.items():
                    args += ["--env", k + "=" + v]
                docker_call(*args, image)
                self.address = docker_call("port", container, "8080").decode().strip()
                deadline = time.monotonic() + 20
                while True:
                    try:
                        result = JsonHttp(
                            origin, "Bearer " + "s" * 40, local_ca_file=str(cert)
                        ).call("GET", "/sre/identity")
                        self.assertEqual(result, {"resource": identity})
                        break
                    except (OSError, RemoteError):
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.2)

            start()
            verification, checks, measured = reviewed(
                d.scope, identity["service.version"], built["image_digest"], config
            )
            vpolicy = VerificationPolicy.parse(verification)
            start_time = datetime.now(UTC).replace(microsecond=0)
            end = start_time + timedelta(seconds=15)
            plan = dict(
                release=release,
                artifact_kind="container-image-v1",
                context_version=0,
                window_start=start_time.isoformat(),
                window_end=end.isoformat(),
                policy_sha256=vpolicy.fingerprint,
                catalog_sha256=vpolicy.catalog_sha256,
            )
            bundle = prepare(
                dict(
                    plan=plan,
                    verification_policy=verification,
                    checks=checks,
                    runner="runner",
                    runner_queue="reference-runner",
                    assessor_queue="reference-assessor",
                    origin=dict(
                        base_url=origin,
                        authorization_env="SRE_JOURNEY_SERVICE_AUTH",
                        ca_env="SRE_JOURNEY_CA_FILE",
                    ),
                    target_service=proof,
                    journey_export=dict(
                        base_url=config.url("tempo"),
                        authorization_env="SRE_JOURNEY_TRACES_AUTH",
                        ca_env="SRE_JOURNEY_TRACE_CA_FILE",
                    ),
                )
            )
            policy = FixturePolicy.parse(bundle["fixture_policy"])
            raw_fixture = bundle["fixture_request"]
            target = JourneyTarget(
                policy.journeys,
                policy.fingerprint,
                {
                    "SRE_JOURNEY_SERVICE_AUTH": "Bearer " + "s" * 40,
                    "SRE_JOURNEY_CA_FILE": str(cert),
                    "SRE_JOURNEY_TRACES_AUTH": tokens["traces_write"],
                    "SRE_JOURNEY_TRACE_CA_FILE": str(stack / "server.crt"),
                },
                d.client("runner"),
                Journal(root / "journeys"),
                policy.target_service,
                policy.journey_export,
            )
            runner = FixtureRuntime(
                policy, d.client("runner"), Journal(root / "runner"), target=target
            )
            assessor = FixtureRuntime(policy, d.client("assessor"), Journal(root / "assessor"))
            executed = runner.execute(raw_fixture)
            # Replacement must preserve request keys, counters and pending exports.
            remove()
            start()
            # Explicit retries through the replacement verify server-side durable keys.
            original_journal = target.journal
            target.journal = Journal(root / "journeys-retry")
            fixture_request = FixtureRequest.parse(raw_fixture)
            for scenario in policy.scenarios:
                target.call("POST", scenario.path, runner.command(fixture_request, scenario))
            target.journal = original_journal
            self.assertEqual(runner.execute(raw_fixture), executed)
            remaining = (end - datetime.now(UTC)).total_seconds()
            if remaining > 0:
                time.sleep(remaining)
            ref = runner.publish(raw_fixture, executed)
            assessment = assessor.assess(raw_fixture, ref)
            query_policy = QueryPolicy(d.scope, tuple(s.profile for s in vpolicy.sources))
            collector_env = json.loads((stack / "collector-env.json").read_text())
            collector = TelemetryCollector(
                query_policy, d.client("collector"), lambda p: transport(p, collector_env)
            )
            from sre_suite.contracts import Release

            release_object = Release(
                d.scope, built["image_digest"], request.binding.plan_digest, ("certification",)
            )
            observations = []
            for source in vpolicy.sources:
                q = source.request(release_object, start_time, end)
                deadline = time.monotonic() + 40
                while True:
                    observation = collector.collect(q, upload_evidence=True)
                    if observation.result.status == "observed":
                        break
                    if time.monotonic() >= deadline:
                        self.fail(str(observation.result))
                    time.sleep(1)
                if source.profile.backend == "prometheus":
                    self.assertEqual(len(observation.result.metrics), 3)
                    for metric in observation.result.metrics:
                        self.assertTrue(all(sample.value == 1 for sample in metric.samples))
                observations.append(
                    dict(
                        source=source.profile.id,
                        evidence=json.loads(canonical(asdict(observation.evidence))),
                    )
                )
            verification_input = {
                "schema_version": 2,
                **plan,
                "fixture": assessment["evidence"],
                "observations": observations,
            }
            verification_input.pop("catalog_sha256")
            engine = CertificationEngine(
                vpolicy, d.client("verifier"), Journal(root / "verification")
            )
            result = engine.run(verification_input, publish=True)
            report = json.loads(d.client("verifier").read(evidence_from_wire(result["evidence"])))
            outcomes = {r["check_id"]: r["outcome"] for r in report["results"]}
            self.assertEqual(len(outcomes), 120)
            for category in ("TRACES", "LOGS", "PROMQL"):
                self.assertEqual(sum(k.startswith(category) for k in outcomes), 40)
            for check in outcomes:
                self.assertEqual(
                    outcomes[check],
                    "PASS" if check in measured else "INCONCLUSIVE",
                    (check, next(r for r in report["results"] if r["check_id"] == check)),
                )
            self.assertNotEqual(report["verdict"], "PASSED")
            self.assertFalse(report["merged"] or report["deployed"])
            print("Built reference certification:", dict(Counter(outcomes.values())))
            self.assertEqual(engine.run(verification_input, publish=True), result)
            for field in ("artifact_digest", "plan_digest"):
                changed = copy.deepcopy(verification_input)
                changed["release"][field] = "sha256:" + "e" * 64
                bad = engine.run(changed)
                self.assertTrue(all(r["outcome"] != "PASS" for r in bad["results"]))
                with self.assertRaises(ValueError):
                    verify(proof, changed, d.evidence, datetime.now(UTC))
            for field, value in (("environment_id", "different-preview"), ("generation", 2)):
                changed = copy.deepcopy(verification_input)
                changed["release"]["scope"][field] = value
                with self.assertRaises(ValueError):
                    engine.run(changed)
            missing = copy.deepcopy(verification_input)
            missing["observations"] = missing["observations"][:-1]
            bad = engine.run(missing)
            self.assertEqual(
                next(r["outcome"] for r in bad["results"] if r["check_id"] == "TRACES-027"),
                "INCONCLUSIVE",
            )
            registry = PreviewRegistry(str(root / "evidence.db"))
            try:
                registry.start(
                    replace(d.f.preview, scope=replace(d.scope, generation=2)), 1, datetime.now(UTC)
                )
            finally:
                registry.close()
            with self.assertRaises(RemoteError):
                engine.run(verification_input, publish=True)
        return True
