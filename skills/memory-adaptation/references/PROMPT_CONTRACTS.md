# Prompt Contracts

Use these contracts for non-trivial tasks. Every contract must include all four sections:
- Required context
- Required constraints
- Verification section
- Done when

## bug-fix-contract.v1

### Required context
- Repro steps and observed vs expected behavior.
- Affected files, interfaces, and environment assumptions.
- Known error signatures or logs.

### Required constraints
- Scope boundaries (what must not change).
- Backward-compatibility requirements.
- Safety/security constraints for touched boundaries.

### Verification section
- Commands for type checks, tests, and bug repro confirmation.
- Evidence artifacts path(s) for planning-gate.

### Done when
- Repro fails before fix and passes after fix.
- No regressions in adjacent behavior.
- Rollback path is documented and validated.

## feature-contract.v1

### Required context
- Objective, users impacted, and data-flow path.
- Existing platform primitives considered for reuse first.
- API/request-response contract (or internal equivalent).
- Dependencies, migration impacts, and estimated scope budget (`files`, `LOC`).

### Required constraints
- Prefer the smallest solution that reuses existing primitives.
- No new service, persistence layer, schema family, or orchestration engine without proof an existing primitive is insufficient.
- Designs over `3` files or `500 LOC` require an explicit justification.
- When the change introduces persisted state, bootstrap/recovery behavior, new public API surface, duplicated materialized state, or new runtime/operator/control surfaces, the plan must include:
  - `contract_closure`
  - `overengineering_guardrails`
- Definition of done categories: correctness, tests, security, observability, rollback.
- Performance and compatibility constraints.
- Explicit out-of-scope boundaries.
- Before presenting the first plan, run a silent plan sufficiency review and revise internally until goals, defaults, interfaces, tests, and assumptions are decision-complete or genuinely blocked.

### Verification section
- Commands for unit/integration/e2e checks.
- Artifact capture commands and output locations.

### Done when
- Acceptance criteria are met with artifact-backed proof.
- Required gates pass deterministically.
- Rollback is executable and tested.
- The first presented plan already has explicit in/out-of-scope boundaries, defaults, acceptance coverage, and surfaced assumptions without requiring a follow-up “look for gaps” prompt.

## frontend-contract.v1

### Required context
- Current UI state (route, screenshot, or Figma selection URL).
- Component ownership and design-system references.
- Behavior expectations for primary user interactions.

### Required constraints
- Stack constraints (framework, styling system, build constraints).
- Mobile requirement (responsive behavior is mandatory).
- Accessibility constraints (focus, contrast, semantic structure).
- Iteration scope constraint: one UI region per iteration.

### Verification section
- Commands for build/test/lint as applicable.
- Responsive checks (mobile + desktop).
- Accessibility checks and visual artifact outputs.
- Frontend roundtrip evidence when Figma context is in scope.

### Done when
- Target region matches constraints and behavior requirements.
- Mobile and accessibility checks pass.
- Visual/acceptance artifacts are produced and linked.

## review-contract.v1

### Required context
- Change summary and risk class.
- Critical paths and externally visible behavior.
- Known sensitive boundaries (auth, input validation, persistence).

### Required constraints
- Prioritize correctness/regression/security findings over style.
- Require concrete file/line references for findings.
- Include missing tests for every high-risk path.

### Verification section
- Commands used for static and runtime checks.
- Evidence showing claimed findings are reproducible.

### Done when
- Findings are severity-ranked with references.
- High-risk gaps include concrete remediation actions.
- Residual risks are explicitly documented.
