---
policy_doc_kind: stop_gate_runbook
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names
---

# Stop-Gate L2 — Completion Record Procedure

Canonical owner of the completion-record procedure, extracted verbatim from `~/.claude/CLAUDE.md` on
2026-06-06; not a duplicate — CLAUDE.md retains the obligation stub only. The obligation ("before
stopping on non-trivial work, file a structured completion record") remains constitutional policy in
CLAUDE.md; this document owns the HOW.

## Procedure

File a structured completion record by piping JSON to `~/.claude/bin/claim_complete.py`. This is the
structured-output companion to the prose summary — both are produced (additive, not record-only). The
stop-gate L2 layer reads `~/.claude/state/cases/${session_id}/completion.json` to validate claims
against recorded tool activity.

Three `kind` values:

- `completion` — task is done. Required: `claim` (one-line summary). Optional but recommended:
  `files_modified`, `commands_run` (with cmd/exit/summary), `slices_completed`, `slices_remaining`,
  `evidence_refs`.
- `blocked` — work cannot proceed. Required: `blocker_type` (one of `external_dependency`,
  `authority`, `direction_fork`) and `description`.
- `fork` — direction conflict requiring user input. Required: `options` (list of `{name, desc}` with
  at least two entries).

Example:

```bash
python3 ~/.claude/bin/claim_complete.py <<'JSON'
{"kind":"completion","claim":"L2 stop-gate shipped","files_modified":["..."],
 "commands_run":[{"cmd":"pytest","exit":0,"summary":"47 passed"}]}
JSON
```

When the stop-gate's `completion_record_required` rule is enabled (off by default until L2 calibration
completes), stops without a completion record block. File the record before the final response, not
after — the Stop hook reads `completion.json` from disk.
