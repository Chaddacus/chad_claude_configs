---
name: refactor
description: Analyze a codebase for quality issues across code smells, clean code, separation of concerns, modularity, and API-first design, then generate a prioritized refactoring report and gated execution roadmap (characterization safety net, commit discipline, delete packet, close-out delta).
context: fork
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names,destructive_rollback,branch_policy_live
---

# /refactor - Codebase Analysis And Refactoring

This skill owns the refactor workflow and its gates. Global policy owns runtime routing, git policy, review requirements, and delivery constraints; `~/.claude/standards/testing-standard.md` owns test-breadth definitions; the `planner` agent owns DAG mechanics.

## Usage

```text
/refactor /path/to/project
/refactor /path/to/project --focus api,modularity
/refactor /path/to/project --report-only
/refactor /path/to/project --depth shallow
```

## Flags

| Flag | Effect |
| --- | --- |
| `(none)` | Full analysis and roadmap generation |
| `--focus X,Y` | Limit the dimensions analyzed |
| `--report-only` | Produce findings without build-spec generation |
| `--depth shallow` | Skip deep architecture tooling |

## Workflow

### 0. Forcing-reason gate

Refactor execution requires a recorded forcing reason — one of:

- blocked feature (name it)
- defect cluster (cite the incidents or issues)
- performance wall (cite the measurement)
- compliance or security finding (cite it)

"Looks legacy" or style preference fails the gate. `--report-only` analysis is exempt; build-spec generation and all execution require the forcing reason recorded in the report header.

### 1. Detect and index

- detect stack and layout
- read local docs that affect architecture
- use RLM for deep analysis when the repo and task justify it

### 2. Analyze five dimensions

For each finding, report:
- what is wrong
- where it is
- severity
- recommended remediation

Dimensions:
1. code smells
2. clean code
3. separation of concerns
4. modularity
5. API-first design

### 3. Heuristic scans → baseline

Gather supporting metrics such as:
- file count
- LOC
- test coverage ratio when available
- largest files
- lint suppressions
- TODO count
- `any` count

Record each metric with the exact command used, as the **baseline table** in the report. The same commands are re-run verbatim at track close (step 8).

### 4. Report

Write:
- executive summary (forcing reason in the header)
- prioritized findings
- architecture overview
- baseline metrics table (commands included)
- phased remediation roadmap

### 5. Safety-net gate (characterization)

Before the first transform lands:

- Every seam the roadmap touches must have behavior-pinning tests (characterization / golden-master) that are green on **unmodified** code. Current behavior includes current bugs — pin it as-is.
- Where coverage is thin, dispatch `test-strategist` proactively. On refactor tracks this inverts its reactive QA-stage role: the safety net is a precondition, not a gap-closure afterthought.
- Each pinning slice's `acceptance_check` is the test command. Every transform packet must list its seam's pinning packet in `blocked_by`; a transform packet with no pinning ancestor for its seam is a planning defect.
- Breadth is governed by `~/.claude/standards/testing-standard.md`: a refactor on an uncovered surface is never smoke-eligible.

### 6. Build specs (planner-consumable packets)

Unless `--report-only`, emit build specs shaped as planner packets — `id`, `goal`, `owned_files`, `blocked_by`, `lane`, `acceptance_check` (a shell command whose exit 0 proves the slice's definition of done) — plus two refactor-specific properties:

- **`mechanical: true|false`** — mechanical transforms (renames, import moves, API call-site migrations, format conversions) are codemod work: `ast-grep` (polyglot), `jscodeshift` (JS/TS), OpenRewrite (JVM). A hand-edited mechanical transform across more than 3 files is a reviewer reject.
- **Terminal delete packet** — required. Enumerates at plan time the old-path symbols, flags, and shims that must be gone at close; its `acceptance_check` is a grep-to-zero command, e.g. `! rg -q 'legacyHandler|LEGACY_ROUTE_FLAG' src/`. The track cannot close while the delete packet is open. A strangler path that never strangles is two systems: any surviving symbol must be named in the close record with a follow-up owner.

### 7. Commit discipline

A slice is exactly one of:

- **behavior-preserving** — characterization tests pass **unchanged**; or
- **behavior-changing** — a separate slice, named as such, with new or updated tests.

Never both in one diff. A mixed diff is a reviewer reject reason.

### 8. Close-out delta

At track close, re-run the exact baseline commands from step 3 and report the before/after table. A close record without the delta table is weak closure under global support-confidence policy.
