---
name: build
description: Execute Chad 2.0's autonomous build protocol for end-to-end implementation work. Use when the task is to spec, scaffold, implement, validate, and package a buildable outcome.
effort: high
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names,destructive_rollback,branch_policy_live
---

# /build - Autonomous Build Protocol

This skill owns the build workflow only. Global policy owns git safety, routing, planning-gate requirements, branch naming, and final review requirements.

## Usage

```text
/build Create a CLI tool that fetches GitHub activity and generates a daily digest
/build Add user authentication with JWT tokens to the Express API
/build --spec-only Implement a billing tracker that monitors email invoices
```

## Flags

| Flag | Effect |
| --- | --- |
| `(none)` | Run the full build workflow |
| `--spec-only` | Stop after spec/contract work |
| `--no-delivery` | Implement and validate, but skip delivery packaging |

## Workflow

### 1. Scope the work

- Small fix: skip heavy scaffolding and go straight to execution.
- Moderate feature: define contracts, then implement in dependency order.
- Large or multi-layer feature: use full spec -> scaffold -> execute -> validate flow.

### 2. Spec and plan

Define:
- data flow
- contracts or interfaces
- acceptance criteria
- task order by dependency

For non-trivial work, use the active planning-gate workflow and prompt-contract source required by global/workspace policy.

### 3. Scaffold when needed

- initialize only the structure the build needs
- install only required dependencies
- ensure the test harness exists before significant implementation work

### 4. Execute

For each task:
1. write the failing test first — if the project has a test harness, the failing test must precede the production code
2. implement the minimum viable change
3. run relevant typecheck, tests, and lint
4. if the same approach fails twice, pivot

### 5. Validate

Confirm:
- no regressions
- data flow is traceable
- solution is not over-engineered
- security basics are covered
- tests cover the change meaningfully

Also remove debug leftovers such as `console.log`, `debugger`, `print()`, `eslint-disable`, and `@ts-ignore` unless justified.

### 6. Delivery

Prepare:
- implementation summary
- validation evidence
- review focus areas
- any branch/commit/PR work required by the current global git policy

If `--spec-only`, stop after the spec is complete.

## Output

Report:
- what was built
- what changed materially
- what was validated
- any remaining blocker or explicit boundary
