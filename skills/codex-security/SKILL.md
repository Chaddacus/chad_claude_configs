---
name: codex-security
description: Security-focused code review of current uncommitted changes using Codex. Checks for injection, auth bypasses, secrets exposure, insecure patterns, and OWASP top 10. Use before committing or shipping security-sensitive changes.
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
---

# /codex-security - Security Audit via Codex

Runs `codex review --uncommitted` with a security-specific prompt targeting vulnerabilities, not style.

## Usage

```text
/codex-security
/codex-security focus on the auth changes specifically
/codex-security --base main
```

## Behavior

Run the following command. If the user provides additional focus text after the command, append it to the prompt:

```bash
codex review --uncommitted "Security audit: identify injection vulnerabilities (SQL, command, XSS), authentication and authorization bypasses, hardcoded secrets or credentials, insecure direct object references, missing input validation at trust boundaries, insecure deserialization, and use of deprecated or vulnerable APIs. Do not report style issues. Flag every finding with severity (critical/high/medium) and the exact file and line."
```

If `--base <branch>` is passed, replace `--uncommitted` with `--base <branch>`.

## Output

Present findings grouped by severity:
- **Critical** — exploitable with no auth or minimal privileges
- **High** — exploitable with standard user access or indirect attack path
- **Medium** — defense-in-depth gap, not directly exploitable in isolation

For each finding: file path, line number, description, and recommended fix.

If no findings: confirm "No security issues found in current changes."

## Notes

- Read-only: this skill does not modify any files.
- For changes already committed to a branch, use `/codex-branch` instead.
