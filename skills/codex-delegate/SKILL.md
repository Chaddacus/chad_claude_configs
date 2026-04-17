---
name: codex-delegate
description: Delegate a concrete implementation task to Codex non-interactively via `codex exec`, then apply the result back to the working tree. Use when you want Codex to actually write code — not just review it. Pairs well with /codex:status and `codex apply`.
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
---

# /codex-delegate - Task Delegation to Codex

Hands a concrete implementation or investigation task to Codex via `codex exec --full-auto`,
captures the session ID, and guides you through applying the result.

Unlike `/codex:rescue` (which is interactive/resumable), this is a fire-and-apply pattern:
Codex runs non-interactively, produces a diff, and you pull it in with `codex apply`.

## Usage

```text
/codex-delegate fix the failing login test
/codex-delegate add input validation to the user registration endpoint
/codex-delegate --model gpt-5.4-mini investigate why the build is flaky
/codex-delegate --dry-run refactor the auth middleware
```

## Flags

| Flag | Effect |
|------|--------|
| (none) | Run with Codex default model and full-auto mode |
| `--model <model>` | Override model (e.g. `gpt-5.4-mini`, `spark`) |
| `--dry-run` | Show the command that would run, but don't execute |

## Workflow

### 1. Confirm scope

Before delegating, verify:
- The task is concrete enough for Codex to act on (not ambiguous direction)
- The working tree is clean or changes are committed (to make `codex apply` clean)
- The task does not require secrets, credentials, or external state that Codex won't have

If working tree is dirty, warn and ask whether to proceed.

### 2. Run delegation

```bash
codex exec --full-auto "<task description>" --json -o /tmp/codex-delegate-last.json
```

If `--model` was specified:
```bash
codex exec --full-auto -m <model> "<task description>" --json -o /tmp/codex-delegate-last.json
```

Capture and display:
- Session ID (from output JSON or `codex resume --last`)
- Summary of what Codex did

### 3. Review the diff

```bash
git diff
```

Show the user what Codex produced. Ask: **Apply, Discard, or Resume in Codex?**

- **Apply** → proceed to step 4
- **Discard** → `git checkout -- .` (warn this is destructive, confirm first)
- **Resume** → show the session ID and tell the user to run `codex resume <session-id>` to continue interactively

### 4. Apply and verify

Run the project's test/typecheck commands to verify the applied changes:
- Detect from `package.json`, `Cargo.toml`, `go.mod`, `pyproject.toml`, etc.
- Run and show results

If verification fails: show the failure, offer to `/codex-delegate` a follow-up fix pass.

### 5. Close

Report:
- What Codex implemented
- Verification result (pass / fail / skipped)
- Session ID (for `codex resume` if the user wants to continue in Codex)

## Notes

- `codex exec --full-auto` operates with workspace-write sandbox — it can modify files but does not push or deploy.
- For long-running or interactive tasks, prefer `/codex:rescue --background` instead.
- To reopen the Codex session and continue interactively: `codex resume <session-id>`
