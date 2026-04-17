---
name: go
description: End-of-task composite workflow — verify (typecheck/tests/lint) → simplify → what-would-chad-do reflection → commit-push-pr. Use when implementation work is code-complete and you are about to call the task done. Not for R1 lookups or mid-implementation checkpoints.
---

# Go — Task Closeout Composite

This skill fuses the end-of-task sequence that CLAUDE.md's Completion section requires. It is one invocation instead of four, closing the gap where a manual closeout skips a step.

## When to invoke

Trigger `/go`:
- Implementation is code-complete (new feature shipped, bug fixed, refactor done)
- Local edits are saved to disk
- About to tell the user "done"

Do NOT trigger `/go`:
- Mid-implementation (use the individual steps instead)
- On R1 factual work with no code changes
- When the worktree has unrelated uncommitted changes (ask first)

## Sequence

### 1. Detect project type

Read the working directory and pick the first matching marker:

| Marker present | Project type | Verify command |
|---------------|-------------|----------------|
| `pyproject.toml` + `uv.lock` | Python (uv) | `uv run pytest && uv run ruff check && uv run mypy .` |
| `pyproject.toml` + `poetry.lock` | Python (poetry) | `poetry run pytest && poetry run ruff check && poetry run mypy .` |
| `pyproject.toml` (neither lock) | Python (pip) | `python -m pytest && ruff check && mypy .` |
| `package.json` + `bun.lockb` | Node (bun) | `bun test && bun run typecheck && bun run lint` |
| `package.json` + `pnpm-lock.yaml` | Node (pnpm) | `pnpm test && pnpm typecheck && pnpm lint` |
| `package.json` + `yarn.lock` | Node (yarn) | `yarn test && yarn typecheck && yarn lint` |
| `package.json` (none of above) | Node (npm) | `npm test && npm run typecheck && npm run lint` |
| `Cargo.toml` | Rust | `cargo test && cargo clippy -- -D warnings && cargo fmt -- --check` |
| `go.mod` | Go | `go test ./... && go vet ./... && gofmt -l .` |

If no marker matches: ask the user for the project's verify command. Do not guess.

If the project has a top-level Makefile with a `check` or `verify` target, prefer that over the table above.

### 2. Run verification

Execute the detected verify command. Three outcomes:

- **Pass** → continue to step 3.
- **Fail, introduced by current changes** → fix in place, re-run verification. Do NOT proceed to simplify or PR with failing tests.
- **Fail, pre-existing (not caused by current changes)** → report to user with the exact failing test name + output, and ask whether to proceed. Pre-existing failures are not a hard stop, but they are a decision point.

Use `git stash` + re-run to distinguish introduced vs pre-existing when unclear.

### 3. /simplify

Invoke `/simplify` via the Skill tool. Apply findings that are clearly correctness or readability improvements. Skip findings that request new abstractions (CLAUDE.md anti-overengineering gate).

### 4. /what-would-chad-do

Invoke `/what-would-chad-do` via the Skill tool. This reflection asks whether there's one more bounded, local, high-leverage step toward the real goal.

- If it yields a concrete next step: take it, then re-run verification, then continue.
- If it clears: continue.

### 5. /commit-push-pr

Invoke `/commit-push-pr` via the Skill tool. Respect CLAUDE.md's git rules:
- Use `codex/` branch prefix
- Never push to `main`
- No `--no-verify`, no `--amend`
- Descriptive PR title and body

### 6. Notify

Run `bash ~/.claude/bin/notify_done.sh --status success --task go --channel desktop`.

## Hard gates

These are not overridable by the user inside `/go`:

- Verification failure caused by current changes blocks the PR. Fix first.
- Dirty worktree with unrelated changes → stop and ask. Never auto-stage unrelated files.
- Pushing to `main` is denied by `settings.json`. Respect it.

## Acceptance criteria

`/go` is complete when:
1. Verify command returned exit code 0 (or user explicitly acknowledged pre-existing failures)
2. `/simplify` was invoked and findings were triaged
3. `/what-would-chad-do` was invoked and either no step was needed or the step was taken + re-verified
4. `/commit-push-pr` opened a PR (URL returned)
5. Notification fired

Report a single-paragraph summary with: verify outcome, simplify findings count, chad-reflection outcome, PR URL.
