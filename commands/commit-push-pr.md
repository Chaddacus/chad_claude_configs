---
description: Run pre-commit checks, create a commit, push to a non-main branch, and open a PR.
argument-hint: "[commit message]"
allowed-tools: Bash, Read
---

# /commit-push-pr

Execute the full PR pipeline. Stop and report if any step fails — never push broken code, never force-push, never push to main.

## Steps

1. **Scope.** `git status`. Confirm there are staged changes, or stage the intended files by explicit path (no `git add -A`). Abort if secrets/env files are staged.
2. **Verify.** Detect the stack and run its checks:
   - `pyproject.toml` / `requirements.txt` → `ruff check . && (pytest -q || python -m pytest -q)`
   - `package.json` → `npm run typecheck --if-present && npm test --if-present`
   - `Cargo.toml` → `cargo check && cargo test --quiet`
   - `go.mod` → `go vet ./... && go test ./...`
   - If none match, run `git diff --check` at minimum.
3. **Branch.** If on `main`/`master`, create a new branch `codex/<short-slug>` from the diff summary. Never commit to main.
4. **Commit.** Build a message from the diff (or the user's `$ARGUMENTS` if provided). Use `git commit` with a HEREDOC message. Do not amend.
5. **Push.** `git push -u origin <branch>`. Never `--force`.
6. **PR.** `gh pr create --title <…> --body <…>` with a summary bullet list derived from the diff and a "Test plan" checklist. Return the PR URL.

## Hard stops
- Any verify step exits non-zero → report the failure, do not commit.
- Branch resolves to `main`/`master` after step 3 → abort, do not push.
- User's `includeCoAuthoredBy: false` already suppresses the Claude trailer. Commit as Chad by default.
