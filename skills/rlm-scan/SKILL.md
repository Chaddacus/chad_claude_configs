---
name: rlm-scan
description: "Codebase scanner — architecture mapping, security scan, pattern search across a repo. Cached index auto-loads on session start so Claude doesn't re-learn the codebase every session."
context: fork
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
---

# RLM Scan

Recursively decomposes a codebase using `rg` + `ast-grep` + `semgrep`, calls Claude
at each level with bounded context, and caches results to project memory. Future
sessions load the summary automatically via the SessionStart hook.

## Invocation

```
/rlm-scan                              # scan current directory, general type
python3 ~/.claude/bin/rlm_scan.py <path> [--type general|security|architecture] [--force]
```

## Scan types

| Type | What it does |
|------|-------------|
| `general` | Architecture, module roles, dependencies, patterns |
| `security` | Runs semgrep (p/default ruleset) + security-focused analysis |
| `architecture` | High-level structure, coupling, layer boundaries |

## When to run

- First time working in a new codebase
- After a major refactor (use `--force`)
- Before a security review (`--type security`)
- When Claude says "I'm not familiar with this repo"

**Do not run on every commit** — the incremental hash check handles file-level changes
automatically. Re-scan only when structure changes significantly.

## Cache location

```
~/.claude/projects/<encoded-path>/memory/rlm_scan_<type>.json
~/.claude/projects/<encoded-path>/memory/rlm_scan_<type>_summary.md
```

## Session auto-load

The `rlm_session_preflight.py` hook fires on `SessionStart` and injects the scan
summary into Claude's context if a cache exists for the current working directory.
Claude sees it as a `[RLM Scan — ...]` notice at the top of the session.

## Interpreting results

- **project_summary**: Overall description; use as mental model anchor
- **key_findings**: Highest-priority items (security issues, architectural concerns)
- **modules**: Per-directory breakdowns with local summaries and findings
- **file_hashes**: Used for incremental re-scan; not directly useful for analysis

## Workflow

When starting work on an unfamiliar codebase:
1. Run `/rlm-scan` on the root
2. Check `key_findings` for known risks before making changes
3. Reference module summaries when navigating to unfamiliar directories
4. Re-scan changed areas with `--force` after significant refactors
