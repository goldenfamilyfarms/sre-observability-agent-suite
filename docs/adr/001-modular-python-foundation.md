# ADR-001: modular typed Python foundation

Status: accepted for the initial foundation. Date: 2026-11-07.

Context: the repository is empty. We need executable contracts and policy tests without assuming external infrastructure is configured.

Decision: Python 3.11+, immutable dataclasses, Protocol integration ports, standard-library unittest and SQLite, strict mypy and Ruff. Runtime dependencies are empty; development tools are pinned. Preserve package data for the catalog when building/installing.

Alternatives: TypeScript provides strong ecosystem integration but adds a runtime/compiler dependency chain for this small policy prototype. A distributed service scaffold would obscure which capabilities actually work. Neither is ruled out for later adapters.

Consequences: the foundation runs offline and is easy to review. Dataclass type annotations do not validate arbitrary JSON; adapters must parse/validate inputs, enforce authorization, and reject unknown fields before construction. Constructors enforce selected domain invariants. SQLite tests demonstrate local persistence only. The repository does not claim a production runtime or distributed durability.
