---
name: test-files
description: Conventions and reminders that apply when editing test files in any language.
paths: ["**/*.test.*", "**/*_test.py", "**/test_*.py", "tests/**/*"]
---

# Test File Conventions

These reminders auto-load when editing test files. They restate (not extend) policy already defined in `~/.claude/CLAUDE.md` under "Verification".

## Rules

- **Scope verification to what changed.** Run the full suite only at task completion, not between slices.
- **Distinguish pre-existing failures from introduced failures.** Pre-existing failures are not your problem; introduced failures are — fix before continuing.
- **No hedging on test outcomes.** State what was run, what the output was, whether it passed or failed. "Should work" / "probably passes" is banned.
- **Don't add tests for the sake of count.** A test that asserts `true` or mocks everything is not verification — it's noise. The reviewer drops it.

## When tests fail after your changes

Fix them immediately. Don't report failure and wait. If 3 attempts to fix don't succeed, change the approach — sunk cost is not a reason to keep going.

## When this file should be updated

When the policy in CLAUDE.md changes. This file is a reminder/restatement layer, not a source of truth.
