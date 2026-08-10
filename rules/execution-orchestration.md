# Execution Orchestration (SPEC.md Standard 3)

## Grounding

Establish current state before consequential decisions, before asking the user to act, and before reporting mutable state.

- **Product truth:** `SPEC.md`, accepted requirements/architecture. **Implementation truth:** current source, tests, schemas, config. **Operational truth:** current GitHub, CI, deployments, external services. Use the authoritative source appropriate to the fact.
- Mutable facts require freshness proportional to how quickly they change.
- Never ask the user to perform an action whose status you can first check with an available authoritative tool.
- Handoffs, memory, old plans, and another agent's assertion are claims, not current truth.
- Ground adversarially: seek evidence that could disprove the current hypothesis.

## Autonomy and escalation

Continue autonomously on any defensible, reversible, evidence-supported decision. Routine permission-seeking is prohibited. Escalate only for: material product-direction decisions unresolved by evidence; destructive or irreversible actions; protected release/promotion gates; critical contradictions; genuine consequential ambiguity. **Uncertainty triggers verification. Material ambiguity may trigger escalation.**

## Triage and resource routing

Before execution, classify scope, complexity, risk, uncertainty, dependencies, and required authority. Use the least expensive adequate model, effort, context, verification, and review. Routing must be defensible. Do not over-model, over-context, over-test, over-review, or over-parallelize.

## Dependency-aware parallelism

Parallelism is an optimization, not the default. Build a dependency graph first: foundational tasks, sequential dependencies, independent work, expected write sets, integration points. Independent code streams use isolated worktrees/branches. Workers produce commits, evidence, and a handoff; a lead owns consolidation and combined verification. Overlapping write sets stay sequential unless explicitly coordinated.

## Adaptive planning

Plans are hypotheses, not scripts. After meaningful actions, compare expected with observed state. If new evidence materially changes assumptions, dependencies, scope, risk, or the best path: re-ground and re-plan. Do not rebuild the plan for minor local errors. Objective, specification, architecture, and grounded reality outrank plan fidelity.

## Independent review

Self-review is required but insufficient for meaningful work. Use a fresh-context adversarial reviewer (the `adversarial-review` skill): give it the requirement/SPEC, integrated diff, and evidence — not the builder's persuasive reasoning. No findings is an acceptable result. Use cross-model review when complexity/risk justifies it. Disagreements resolve with evidence, tests, contracts, SPEC, and live behavior — never model prestige.

## Provenance

For consequential work, preserve compact inspectable provenance: sources checked, explicit assumptions/decisions, actions taken, verification actually run, review findings, unresolved risks. Do not record private chain-of-thought.
