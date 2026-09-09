# Verification & Evidence (SPEC.md Standard 4)

**Every correctness claim requires evidence capable of observing that claim.**

## Tiers

- **FAST** — cheap breadth heartbeat: app starts, critical config, vital capabilities respond, essential API/MCP surfaces, critical dependencies reachable. Cheap enough to run frequently.
- **MODULE** — deep verification of the affected capability: unit/domain, integration, persistence, API/MCP contract, regression/security cases, relevant browser proof, AI evals if the module contains AI.
- **FULL** — integrated application confidence: release, promotion, architecture change, high-risk or major-dependency work. Never for trivial changes.

Projects declare stack-specific commands per tier in `.claude/verification.json` (the foundation defines semantics; see the `verify-fast`/`verify-module`/`verify-full` skills). Consequential runs emit a verification manifest; completion evidence must be current for the final diff.

## Test principles

- Tests protect behavior, contracts, boundaries, meaningful risk, and regressions — count and coverage percentage are not the objective. Each test defends its existence.
- Test behavior over internal implementation where practical. Use the cheapest layer that can prove the property. Inspect existing tests before writing new ones.
- One behavior has one primary proving layer; other layers test their own boundary, not the same assertion.
- Do not rerun flaky tests until green and report success — flakiness is a defect to investigate.
- Mock external boundaries where useful; do not mock your own business logic so heavily the test only proves mocks agree.

## Frontend evidence

Meaningful frontend changes require browser-grounded proof (Playwright or equivalent): real interaction, runtime/console/network health, accessibility structure, selected screenshots when appearance matters. **A screenshot file existing is not verification — it must be inspected.** Use the `ui-proof` skill.

## Evidence lifecycle

Routine generated screenshots/traces/videos belong in CI artifact storage, not Git. Git keeps intentional fixtures, baselines, schemas, tests, source. Retention defaults: FAST failure artifacts ~7d, MODULE ~14d, FULL ~30d, significant release evidence up to ~90d when justified.
