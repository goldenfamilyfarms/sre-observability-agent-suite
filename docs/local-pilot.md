# Local pilot startup package

Version 0.9 provides an explicit local lifecycle for PostgreSQL, persistent Temporal development state, a loopback HTTPS evidence API, migrations, separate scoped credentials and the existing engineering worker. This is a single-operator development setup for a trusted POSIX host, not a production deployment. No Jira ticket or workflow is created by initialization or startup. No cloud environment, merge, promotion or deployment is performed.

## Components and prerequisites

| Component | Local behavior |
| --- | --- |
| PostgreSQL 16/17 | Digest-pinned Docker image, password read from a private mounted file, named volume, published only on `127.0.0.1` |
| Temporal | Operator-installed CLI `server start-dev`, loopback RPC, UI disabled, SQLite state retained under the private pilot directory |
| Evidence API | Local-only reference WSGI host terminating TLS itself; checksummed migrations required; separate exact-scope controller and worker grants |
| Engineering worker | Host Python process using the existing Temporal worker; host Git, Claude CLI and Docker remain available without mounting a Docker socket in a worker container |
| Persistence | PostgreSQL volume, Temporal SQLite state and private execution journal survive normal stop/restart |

Install Python 3.11+, Git, OpenSSL, Docker and a Temporal CLI. The package does not install binaries or pull images during startup. The tested Temporal CLI is 1.10.0 (server 1.32.2); use a compatible reviewed version and retain it with the local state. The CLI's SQLite database is a development persistence mechanism, not a production cluster or disaster-recovery design. The [official CLI guide](https://docs.temporal.io/cli) and [server command reference](https://github.com/temporalio/documentation/blob/main/docs/cli/command-reference/server.mdx) describe `--db-filename` and `--headless`.

```sh
python -m pip install '.[postgres,orchestration]'
docker pull postgres@sha256:748a18a54d3842d43bc156f0840d78029b8d349e0feca98c12b9c4ce3404a6b3
```

The supplied image is PostgreSQL 17. The launcher checks the running server major version and accepts only 16/17, whose data-directory layout matches the volume mount. Do not silently change image or Temporal versions against retained data. See the [official PostgreSQL image](https://hub.docker.com/_/postgres) for password-file handling and database upgrade requirements. Keep Docker current: [published loopback ports](https://docs.docker.com/engine/network/port-publishing/) are not a substitute for a trusted host/daemon.

## Initialize one explicitly authorized scope

Prepare a current [pilot manifest](pilot-setup.md#manifest) outside the repository. Select the real repository ID, PR number, source revision, preview generation, Jira key and baseline time/digest. `init` explicitly provisions local controller/worker grants for **that exact scope and validity window**; it rejects production, invalid preview IDs, mismatched expected generations, expired/future windows and invalid baseline metadata. It does not upload evidence, register the preview or inspect the baseline bytes. The later `pilot-setup` step verifies the bytes.

Create a private parent outside any Git checkout, using physical absolute paths without symlink components. The following paths are placeholders; substitute your own existing private location. The `stack` directory must not exist.

```sh
mkdir -m 700 /absolute/private/sre-pilot
python -m sre_suite.local_pilot init \
  --directory /absolute/private/sre-pilot/stack \
  --manifest /absolute/private/manifest.json \
  --postgres-image postgres@sha256:748a18a54d3842d43bc156f0840d78029b8d349e0feca98c12b9c4ce3404a6b3 \
  --temporal-binary /absolute/path/to/temporal
```

Optional `--postgres-port`, `--temporal-port`, `--api-port` default to 55432, 7233 and 8443. They must be distinct unprivileged ports. Binds are always `127.0.0.1`; there is no public bind flag. Initialization generates high-entropy independent database/controller/worker secrets and a seven-day self-signed certificate for localhost/127.0.0.1. Files are owner-only and the complete directory is published atomically. An existing directory is never overwritten or rotated by re-running init.

The private stack contains `stack.json`, the manifest copy, digest-only `credentials.json`, separate token/password files, `server.crt`, `server.key`, `journal/` and `temporal/`. Back up this state together with the PostgreSQL volume. Keep every file out of source control. Credentials expire at the manifest's preview expiry; certificate lifetime does not extend grants. Stop and explicitly reprovision/review scope for a new pilot, or perform a deliberate private credential configuration update. Nothing renews itself.

## Start services and worker

First, services can run without Jira/model credentials:

```sh
python -m sre_suite.local_pilot up --directory /absolute/private/sre-pilot/stack
```

The foreground supervisor checks ports and the cached database image, starts its PostgreSQL container, waits for SQL readiness, explicitly applies migrations 1 and 2, starts Temporal, waits for the namespace, then starts the HTTPS API and checks TLS/database readiness. It prints a redacted ready record. A local file lock prevents concurrent starts of the same stack. It does not initialize previews or start workflows.

For a worker, copy [examples/local-worker.json](../examples/local-worker.json) to a private file, set mode 0600 and fill in the real Jira/model/repository settings. Values are strings, including JSON encoded allowlists and test commands. This file is parsed as JSON, never sourced as shell code. The launcher supplies evidence credentials, certificate trust, journal, isolated Temporal namespace and queue; the worker file cannot override them. Ambient Jira/model/database credentials are not inherited. The controller token and database password are never put in the worker's environment.

```sh
chmod 600 /absolute/private/worker.json
python -m sre_suite.local_pilot up \
  --directory /absolute/private/sre-pilot/stack \
  --worker-env /absolute/private/worker.json
```

Use either services-only `up` or the combined command, not both. Stop services-only startup with Ctrl-C before switching to combined startup. Alternatively run a separate worker terminal against already-running services:

```sh
python -m sre_suite.local_pilot run \
  --directory /absolute/private/sre-pilot/stack \
  --worker-env /absolute/private/worker.json -- worker
```

Combined startup additionally waits for workflow and activity pollers. A polling worker may execute previously queued work when resumed; startup creates no new workflow. Keep the namespace private to this trusted host. Temporal development RPC has no authentication and is suitable only for this local trust boundary. Readiness is an observation, not authorization to run a ticket or proof of model access.

Model calls, Jira writes, GitHub PRs and Jira transitions default to disabled in the example. Enable only the intended live-pilot actions after supplying real configuration; use the [publication guide](pr-and-jira-publication.md) for transition IDs and permissions. Pre-pull your digest-pinned test image and ensure it contains test dependencies. Neither the worker nor the launcher downloads test images at runtime.

## Prepare and run one ticket separately

From a second terminal, with services running:

```sh
python -m sre_suite.local_pilot check \
  --directory /absolute/private/sre-pilot/stack --worker

python -m sre_suite.local_pilot run \
  --directory /absolute/private/sre-pilot/stack -- pilot-setup \
  /absolute/private/manifest.json --baseline /absolute/private/baseline.bin \
  --output /absolute/private/sre-pilot/prepared
```

Put the returned evidence metadata into the matching Jira JSON contract. Then run preflight:

```sh
python -m sre_suite.local_pilot run \
  --directory /absolute/private/sre-pilot/stack \
  --worker-env /absolute/private/worker.json -- preflight \
  /absolute/private/sre-pilot/prepared/execution.json
```

Resolve failures and review unverified capabilities. Preflight exit 2 still means live inference/writes remain unverified. After separately authorizing that ticket execution, replace `preflight` with `start`. Use `status WORKFLOW_ID`, `result WORKFLOW_ID` or `cancel WORKFLOW_ID` through the same wrapper. `run` accepts only existing execution subcommands, preserves their exit codes, and does not print or write a shell file containing secrets.

The local evidence client uses `SRE_EVIDENCE_CA_FILE` to trust this stack's certificate explicitly. No OS/browser trust store is modified and TLS verification is never disabled. Nonstandard HTTPS ports and this certificate override are permitted only for literal `127.0.0.1` or `localhost`. Existing remote endpoints retain their HTTPS/443 rules. Model, Jira and GitHub clients retain normal platform trust. The local certificate digest is included in runtime policy fingerprints so a changed trust anchor cannot silently resume an old local execution policy.

## Stop, restart and recovery

Ctrl-C or SIGTERM requests shutdown. The supervisor stops its worker, HTTPS host and Temporal process groups, then its own PostgreSQL container. It preserves the named volume, credentials, certificate, Temporal database and execution journal. Run the same `up` command to restart; migrations verify checksums and do not reset state. The supervisor never prunes Docker, removes unrelated containers/volumes, or deletes persisted data. Unexpected child exits or a stopped database fail the supervisor and trigger cleanup; it does not silently restart a failing worker or launch a new execution.

The supervisor must remain running. Forced process/host termination or an uncertain Docker response can leave a container behind. A subsequent start fails on occupied ports or an existing container name rather than adopting it. Inspect the exact `sre-local-<identity>` container and its `sre.pilot.id` label from `stack.json`, then explicitly stop that matching container before restarting. Do not remove its `sre-local-<identity>-postgres` volume unless deliberately discarding that pilot's database. An existing volume is reused only when its label matches the stack identity. Cleanup errors are surfaced as failure, so inspect owned resources after a failed shutdown.

Use `check` to inspect SQL, Temporal namespace and verified HTTPS readiness; `--worker` additionally checks poller presence. It does not create a workflow or read Jira/model credentials. Poller presence is not a lease and can briefly reflect a worker that has stopped. Expired/revoked grants can leave infrastructure healthy while execution remains unauthorized; preflight and API scope checks enforce access separately.

`credentials.json` is reloaded for every API request. An invalid reload fails closed; a removed digest is no longer accepted on subsequent requests. Use atomic private-file replacement when changing credentials. An empty credential list revokes all access. In-flight requests retain the authenticator's existing before-commit checks, but a file edit alone cannot retract an already executing request until the next reload. This local single-process behavior is not distributed revocation.

The API is a bounded local reference host based on Python's [WSGI reference implementation](https://github.com/python/cpython/blob/main/Lib/wsgiref/simple_server.py), not a production server. It terminates TLS directly, rejects duplicate authentication/framing headers and transfer encoding, sets socket timeouts and suppresses raw request/exception logs. Health responses contain no credentials or scope data. There are no public DNS, certificate issuance, automatic certificate renewal, durable access audit or production hosting adapters. Same-host users and administrators are within this pilot's trust boundary.

## Validation

Default tests cover private initialization, exact grants, credential separation/reload, immutable state, strict ports/images/paths, environment isolation, loopback-only certificate overrides, locks, port conflicts, startup failure cleanup and redacted errors. The explicit local-stack suite starts real PostgreSQL, Temporal, HTTPS and the actual Temporal worker with synthetic external configuration. It verifies real TLS upload/read, API credential revocation, stale-generation denial, PostgreSQL migrations/evidence persistence, Temporal workflow identity across full shutdown/restart, and refusal of untrusted TLS/plaintext/ambiguous HTTP framing.

```sh
SRE_TEST_POSTGRES_IMAGE='postgres@sha256:748a18a54d3842d43bc156f0840d78029b8d349e0feca98c12b9c4ce3404a6b3' \
SRE_TEST_TEMPORAL_BINARY='/absolute/path/to/temporal' \
python -m unittest discover -s local_pilot_tests -v
```

These tests remove only their own verified test volume. They make no Jira/GitHub/model calls. They prove local startup and restart, not live ticket completion, power-loss recovery, database upgrades, independent reviewer identities or production readiness.
