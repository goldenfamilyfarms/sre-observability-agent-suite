# ADR-002: external durable orchestration and Claude Code workers

Status: architectural boundary accepted; Temporal accepted for a bounded pilot in ADR-010. Date: 2026-11-07.

Decision: use Claude Code for engineering work and an external durable orchestrator for state, dependencies, retries, timers, approval waits and recovery. Temporal is implemented for the bounded pilot described in [ADR-010](010-temporal-execution-pilot.md). Do not store authoritative workflow progress solely in Claude sessions.

Rationale: Temporal documents durable workflow execution and replay. Claude Code documents programmatic execution with structured output and tool controls. These support the intended separation; they do not establish that our integration works. See [Temporal execution](https://docs.temporal.io/workflow-execution) and [Claude Code programmatic execution](https://code.claude.com/docs/en/headless), consulted during Phase 0.

Evaluation exit: prove replay determinism, worker crash/restart, activity heartbeats and cancellation, duplicate delivery, bounded retries, approval signal authentication, versioned workflow upgrades, and recovery after an external write succeeds but its response is lost. Keep network/process I/O in activities, not replayed workflow code. Assess hosting, operating cost and ownership before selecting a service.

Alternatives: an existing organization's durable engine could be used after the same proof; a prompt loop or ad-hoc queue is insufficient for long-lived execution state. No infrastructure choice is assumed from an unrelated environment.

Consequences: the original foundation provided pure policy and local checkpoints. Increment 4 adds the real Temporal/Claude/Jira adapters and engine integration tests in ADR-010. SQLite reopen tests alone remain insufficient proof. Production approval authentication, workflow upgrades and operational hosting are still open.
