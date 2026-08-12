# Architecture Contract (SPEC.md Standard 2)

Governing question: **who owns this code, why does it belong here, and what contract exposes it?**

## Spine + modules

Applications use a deliberately small spine plus capability modules.

- The spine owns only true application-wide coordination: startup/bootstrap, configuration wiring, dependency registration, top-level routing composition, global middleware/providers, cross-module orchestration. The spine MUST NOT become a business-logic module.
- Business capabilities belong to vertically coherent modules (e.g. `src/modules/projects/`), not to global technical-layer directories.
- Each module owns: its domain behavior, its data and persistence abstraction, internal implementation, feature-specific validation, its public capability contract, and its feature-specific tests/evals/proof.
- Cross-module consumers MUST use explicit public contracts. They MUST NOT reach into private implementation or directly mutate another module's owned data. Cross-module workflows belong in the spine or another explicitly owned workflow capability.

## API/MCP first

- Core business capabilities exist independently of any UI, REST route, or MCP tool. REST/API and MCP are adapters over the same module contracts.
- Business logic MUST NOT be duplicated between API and MCP.
- Where practical, request/response/domain schemas have one canonical definition reused or generated across adapters.

## Canonical specification

- Every repository has a root `SPEC.md`: the canonical current specification — purpose, accepted requirements, architecture, module ownership, capability contracts, data ownership, accepted implementation state, known limitations, security/trust boundaries.
- Supporting documentation lives under `docs/` and is subordinate to `SPEC.md`.
- Historical plans/handoffs MUST NOT outrank current `SPEC.md` or current live system state.

## Testing organization

Tests have one intentional top-level structure (e.g. `tests/{unit,integration,contract,e2e,fixtures}`). Stack conventions may vary; the repository exposes one coherent testing model.
