---
name: migration-files
description: Safety reminders that apply when editing database migration files.
paths: ["**/migrations/**/*", "**/*migration*.sql", "**/*migration*.py", "**/*migration*.ts"]
---

# Migration File Safety

These reminders auto-load when editing migration files. They restate (not extend) policy already defined in `~/.claude/CLAUDE.md` under "Safety And Git Rules".

## Rules

- **Never use destructive operations** without explicit user request: `DROP TABLE`, `TRUNCATE`, irreversible column drops, data deletion.
- **Migrations are forward-only by default.** Down/rollback paths require justification — most production migrations don't get rolled back; they get superseded by a new forward migration.
- **Backfills on large tables need batching.** A `NOT NULL` column added with a default to a 50M-row table will lock writes if not batched. Reviewer Task 4.6 (independent review) applies — get a second opinion on lock behavior before shipping.
- **Test against a real database, not a mock.** Mock-DB tests pass while production migrations fail; this is a documented past failure mode in this workspace.

## What to do if a migration is risky

Pause and ask the user. Migration safety is one of the explicit "ask before acting" cases — this is not a default-to-action scenario.
