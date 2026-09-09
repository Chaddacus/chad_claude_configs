# Independent review

## Scope and evidence

Use the read-only `reviewer` role at meaningful feature checkpoints and before promotion to main or production, as required by [development.md](development.md). Group small related edits into one review.

Give the reviewer the original requirements, application `spec.md` where applicable, exact change scope and revision, and verification evidence. For uncommitted changes or files outside Git, identify the actual files and reviewed snapshot. The reviewer inspects the changes, relevant callers, and results directly; an implementing agent's summary is not proof. Include relevant paths and concise verification results so the reviewer can locate complete evidence without reconstructing the handoff. Do not repeatedly reload unchanged source without a concrete review reason.

Read applicable guidance through global `CLAUDE.md`. Stay read-only: report findings rather than editing code, changing board status, merging, or deploying. Use non-mutating checks where useful and state when runtime evidence is unavailable.

## Prepare a self-contained review

For a bounded independent review, default to a fresh session without the builder's conversation history when a complete assignment can convey the necessary context. Use the client's explicit fresh-context option where available; do not assume spawning a new agent excludes inherited history. In Codex delegation, use `fork_turns="none"` for this case. Include selected history or inherit context when essential decisions cannot be conveyed accurately in the assignment, and explain the need briefly.

Supply the authoritative requirements and relevant spec sections, workspace, base and head revisions or hashes for uncommitted files, changed paths and relevant callers, verification commands and their results with evidence paths, actual review concerns, scope boundaries, and known missing information. State whether evidence covers only a subset. Do not describe the builder's conclusion as an established fact.

For a small, already-known set of source files, prefer a direct initial read batch. Do not create a separate all-source evidence bundle by default; use one when reuse, snapshot preservation, or otherwise repeated retrieval justifies it. Source already present and current need not be fetched again solely to change its presentation.

Select guidance by the change's actual concerns. Name relevant guides or sections; do not ask every reviewer to reload all implementation and delivery guidance. Read necessary task evidence and guidance together where practical. Retrieve further code, requirements, or guidance when a concrete finding or uncertainty requires it; scope limits must not suppress related defects or necessary verification. Reuse already available unchanged guidance rather than rereading it merely because a new step begins.

For bounded handoffs, materialize the relevant guidance with `python3.11 ~/.claude/scripts/read-guidance.py` and exact section selectors. Supply the resulting excerpts with their source paths and hashes. If current excerpts are already in context, apply them directly; do not reread complete guides merely to reconstruct the same instructions. If a source changes or a necessary section is missing, retrieve that material. Hashes identify the snapshot; they do not prove the source is correct or trustworthy.

The reviewer inspects source and evidence independently. If essential context is missing, request the specific information instead of reconstructing an entire project history. A complete handoff reduces retrieval work; it does not replace verification.

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
