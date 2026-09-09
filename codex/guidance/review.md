# Independent review

## Scope and evidence

Use the read-only `reviewer` role at meaningful feature checkpoints and before promotion to main or production, as required by [development.md](development.md). Group small related edits into one review.

Give the reviewer the original requirements, application `spec.md` where applicable, exact change scope and revision, and verification evidence. For uncommitted changes or files outside Git, identify the actual files and reviewed snapshot. The reviewer inspects the changes, relevant callers, and results directly; an implementing agent's summary is not proof. Include relevant paths and concise verification results so the reviewer can locate complete evidence without reconstructing the handoff. Do not repeatedly reload unchanged source without a concrete review reason.

Read applicable guidance through global `AGENTS.md`. Stay read-only: report findings rather than editing code, changing board status, merging, or deploying. Use non-mutating checks where useful and state when runtime evidence is unavailable.

## What to check

Scale review to the change and its consequences:

- Requirements and correctness: intended behavior, boundary validation, errors, caller compatibility, and regressions.
- Code construction: clear contracts, coherent responsibilities, explicit dependencies and state, accurate file and function documentation, and complexity justified by actual needs.
- Architecture: a small coordinating spine, cohesive modules, meaningful plugin boundaries, no business logic duplicated across UI/API/MCP, and no circular dependencies or access to another component's internals. Inspect delegated helpers for displaced monoliths.
- Lifecycle, when affected: ownership, partial startup failure, cleanup, cancellation, dependency availability, and external effects that cannot simply be reversed.
- Verification and delivery: meaningful behavior and user-workflow tests, appropriate API/MCP and permission checks, evidence for the current integrated revision, and accurate `spec.md`. Apply the required promotion and recovery checks when reviewing a release.

Use [coding.md](coding.md), [architecture.md](architecture.md), [lifecycle.md](lifecycle.md), and [development.md](development.md) for their detailed standards. Review configuration or documentation changes against their actual requirements without inventing application-only requirements.

## Findings and resolution

Write concise standard technical English. Each substantive finding needs a precise location, supporting evidence, practical consequence, and recommended fix. For structural problems, identify the responsibility that should move and why its current placement causes a concrete problem. Ground findings in inspected code, a counterexample, a relevant test result, or a specific unmet requirement. Recheck cited locations before reporting.

Separate defects and unmet acceptance criteria from optional suggestions. Do not block on personal taste, prescribe arbitrary file-length limits, or invent findings to meet a quota. State the scope, revision or snapshot, and verification limitations even when no substantive findings are found.

The implementer resolves substantive findings; the supervisor verifies fixes against the current revision and checks related regression risk. After fixes, start with the changed code and affected behavior, expanding when the changes introduce broader risk. Avoid repeating clean checks or expanding into unrelated audits without new evidence. If disagreement persists without new evidence, explain the disputed decision to the user instead of looping.

The lead remains accountable for integration and completion. If independent review is unavailable, report the limitation. Review complements automated checks: a clean review does not prove unexecuted tests passed or authorize merging or deployment.
