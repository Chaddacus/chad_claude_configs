---
name: refactor
description: Analyze a codebase for quality issues across code smells, clean code, separation of concerns, modularity, and API-first design, then generate a prioritized refactoring report.
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names,destructive_rollback,branch_policy_live
---

# /refactor - Codebase Analysis And Refactoring

This skill owns refactor analysis workflow only. Global policy owns runtime routing, git policy, review requirements, and delivery constraints.

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

### 3. Heuristic scans

Gather supporting metrics such as:
- file count
- LOC
- test coverage ratio when available
- largest files
- lint suppressions
- TODO count
- `any` count

### 4. Report

Write:
- executive summary
- prioritized findings
- architecture overview
- metrics
- phased remediation roadmap

### 5. Optional build specs

Unless `--report-only`, generate implementation-ready phase specs with:
- task description
- files or subsystems involved
- acceptance criteria
- dependencies on prior phases
