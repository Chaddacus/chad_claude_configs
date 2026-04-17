---
name: codex-branch
description: Full branch review via Codex — reviews all changes on the current branch vs a base branch (default main). Use before opening a PR to get a complete review of everything that will be merged.
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
---

# /codex-branch - Branch Review via Codex

Runs `codex review --base <branch>` to review all commits on the current branch vs a base.
Wider scope than `/codex:review` (uncommitted only) — covers the full diff that will appear in a PR.

## Usage

```text
/codex-branch
/codex-branch --base develop
/codex-branch challenge the caching design
```

## Behavior

1. Detect the current branch:
   ```bash
   git branch --show-current
   ```

2. Determine the base. Default is `main`. If `--base <branch>` is passed, use that value.

3. Run the review:
   ```bash
   codex review --base <base-branch>
   ```
   If the user provided additional focus text, append it as the prompt argument.

4. If the branch has no commits ahead of the base, tell the user and stop.

## Output

Present findings as a structured review:
- **Summary**: what the branch does (1–2 sentences)
- **Correctness issues**: bugs, broken edge cases, logic errors
- **Design concerns**: whether the approach fits the problem; simpler alternatives
- **Security**: any security-relevant patterns
- **Missing**: tests, error handling, documentation gaps that matter

Close with a **Ship / Fix first / Discuss** verdict:
- **Ship** — no blocking issues
- **Fix first** — specific items must be addressed before merge (list them)
- **Discuss** — design questions that need a decision before proceeding

## Notes

- Read-only: this skill does not modify any files.
- For uncommitted-only review, use `/codex:review`.
- For security-specific audit, use `/codex-security --base <branch>`.
