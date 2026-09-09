# Reviewer Bash Guard (Deferred Work)

## What this is
A tracking stub for a not-yet-implemented `PreToolUse` hook scoped to reviewer agents that would block `Bash` invocations attempting to mutate files inside the reviewed worktree.

## Why it's not done
The reviewer trio (`reviewer.md`, `python-reviewer.md`, `typescript-reviewer.md`) has `Write`, `Edit`, and `Task` removed from their tool lists (cert Foundations Task 4.6 — independent review instances). This prevents direct file mutation through SDK tools and prevents delegation to mutating subagents.

It does NOT prevent mutation via `Bash` (shell redirection, `sed -i`, `git apply`, etc.). The reviewer's body contract is return-text-only; a Bash mutation would be a contract violation, not an intended workflow.

## What a real implementation looks like
A `PreToolUse` hook with `matcher: "Bash"` that, when the active agent is in the reviewer trio AND the command parses as a mutation (write redirect, `sed -i`, `cp`, `mv`, `rm`, `git apply`, `git commit`, etc.), denies the call with a structured error.

## When to do it
When a reviewer is observed mutating the reviewed worktree in production. Not before — premature enforcement burns trust and the current contract is voluntary.
