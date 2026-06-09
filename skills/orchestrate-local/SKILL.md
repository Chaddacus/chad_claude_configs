---
name: orchestrate-local
description: DEPRECATED 2026-04-22 — use `/drive --local-worker` instead. This skill redirects to the consolidated entry point. Scheduled for removal after 2026-05-22 soak period. Full semantics preserved behind the --local-worker flag on /drive.
---

# /orchestrate-local — DEPRECATED (redirect to `/drive --local-worker`)

> **Deprecation notice (2026-04-22):** this skill has been consolidated into `/drive --local-worker`. Every workflow previously documented here is now in `~/.claude/skills/drive/SKILL.md` under the "Phase 2 (--local-worker)" block. Soak window: 30 days. Slated for removal after 2026-05-22 unless invocations show it's still being used.

## What changed

The two-tier loop (Claude supervises, goose/qwen executes on local 4090, verify-script decides) is unchanged. Only the invocation changed:

| Before | After |
|---|---|
| `/orchestrate-local <goal>` | `/drive --local-worker <goal>` |

## What was preserved

All of it — preflight check, slice discipline, acceptance-script-before-dispatch rule, gate calibration, outcome handling (pass/fail/escalate/infra_down/gate_cheat_suspected), 3-supervisor-takes escalation budget, Phase 3.5 user-journey smoke, memory write-back. Read the `/drive` SKILL.md's `Phase 2 (--local-worker)` section.

## Why this was consolidated

Orchestration audit at `~/.claude/reports/orchestration-audit-2026-04-21.md` found `/orchestrate-local` and `/drive` differ only in worker backend — every other block was identical. Five skills were writing indistinguishable state schemas to `~/.claude/state/autonomy/`. Folding this one into `/drive --local-worker` reduces the orchestration surface without losing behavior.

## If you invoked this skill

Re-issue as `/drive --local-worker <your goal>`. If that fails, file a blocker — do not paste the old invocation back.

## Backup of original SKILL.md

`~/.claude/backups/2026-04-22-consolidation/skills/orchestrate-local/SKILL.md`
