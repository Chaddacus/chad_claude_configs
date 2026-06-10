---
name: fix-issue
description: DEPRECATED 2026-04-22 — use `/drive --issue <url>` instead. This skill redirects to the consolidated entry point. Scheduled for removal after 2026-05-22 soak period. TDD discipline and issue-intake semantics preserved behind the --issue flag on /drive.
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names,destructive_rollback,branch_policy_live
---

# /fix-issue — DEPRECATED (redirect to `/drive --issue <url>`)

> **Deprecation notice (2026-04-22):** this skill has been consolidated into `/drive --issue <url>`. The issue-intake prelude (fetch, sanitize, parse into goal) and TDD-first planning discipline are preserved in `/drive`. Soak window: 30 days. Slated for removal after 2026-05-22 unless invocations show it's still being used.

## What changed

| Before | After |
|---|---|
| `/fix-issue https://github.com/owner/repo/issues/123` | `/drive --issue https://github.com/owner/repo/issues/123` |
| `/fix-issue "The login button doesn't respond on mobile"` | `/drive <description>` (no --issue needed for text input) |
| `/fix-issue --dry-run <url>` | Not carried over — ask Claude to read the issue first, then plan without implementing |

## What was preserved

- Issue intake: `gh issue view` fetch, prompt-injection sanitization, body truncation, task-string formation as `"fix issue #<num>: <title>"`.
- TDD-first planning: first slice writes a failing test that reproduces the bug; second slice implements the minimal fix; then re-run suite + lint.
- Debugging escalation: if 3 same-area attempts fail, stop and re-evaluate architecture — no 4th same-level attempt.
- Global git policy compliance for any branch/commit/PR work.

Read the `/drive` SKILL.md, specifically:
- Phase 0 `--issue <url>` block (fetch + sanitize + task formation)
- Phase 1 `If --issue` block (TDD-first slice ordering)

## Why this was consolidated

Orchestration audit at `~/.claude/reports/orchestration-audit-2026-04-21.md` identified `/fix-issue` as the oldest autonomous-loop skill (SKILL.md mtime 2026-03-18) with the thinnest unique logic — fetch issue, parse, then drive. Folding into `/drive --issue` removes a separate entry point while preserving the issue-specific prelude behind a flag.

## If you invoked this skill

Re-issue as `/drive --issue <url>` for URL input, or plain `/drive <description>` for text input.

## Backup of original SKILL.md

`~/.claude/backups/2026-04-22-consolidation/skills/fix-issue/SKILL.md`
