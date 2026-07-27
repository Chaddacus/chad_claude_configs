---
policy_doc_kind: ralph_loop_runbook
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names
---

# Ralph Loop Runbook (v1.4)

## Scope
Operational runbook for Postflight Completion Gate (Ralph Loop) enforcement on `R3/R4` routes.
Auto-continue is enforced for `codex exec` finalize flows.

## Files
- Wrapper: `~/.claude/bin/claude_run`
- Gate engine: `~/.claude/bin/ralph_done_loop.py`
- CI predicate checker: `~/.claude/bin/postflight_acceptance_check.py`
- Route telemetry extractor: `~/.claude/bin/route_audit_extract.sh`
- Config: `~/.claude/state/route_manifest.json`

## Modes
- `audit`:
  - executes gates
  - writes telemetry/artifacts
  - does not block acceptance
- `enforce`:
  - gate exit codes are blocking
  - CI predicate is merge/acceptance blocking
  - wrapper auto-continues on `revise` until `approve` or hard-stop

## Exec Contract
`claude_run` auto-continue supports only:
- `codex exec "<prompt>"`
- `codex exec -` (prompt from stdin)

For `--finalize-attempt` with `R3/R4`, non-`exec` invocation is rejected:
- `reason_code=UNSUPPORTED_EXEC_MODE_FOR_AUTOCONTINUE`
- `exit_code=30`

## Rollout
1. Bind default `codex` entrypoint to wrapper shim:
   - `which codex`
   - expected: `~/.local/bin/codex`
2. Set `postflight.mode` to `audit`.
3. Run for a short canary window and collect `ralph-meta` telemetry.
4. Confirm parse errors and branch predicate failures are zero (or explained).
5. Keep/switch `postflight.mode` to `enforce`.

## Verify Commands
```bash
# Verify wrapper binding and real binary target.
which codex
echo "$CLAUDE_HOME"

# Script syntax checks.
bash -n ~/.claude/bin/claude_run
python3 -m py_compile ~/.claude/bin/ralph_done_loop.py

# Focused reliability tests.
pytest -q ~/.claude/tests/postflight  <!-- pointer-check:skip -->
```

## Canary Checks
1. Success branch:
   - `planning_gate_finalize` emits `status=approve`
   - `finalize.accepted.json` exists
   - checker exits `0`
2. Blocked branch:
   - final status blocked with acceptable reason code
   - `finalize.blocked.json` exists
   - checker exits `0` in enforce mode only when blocked branch is internally consistent
3. Lock behavior:
   - second concurrent run for same `route_task_id` yields `LOCK_BUSY`
   - stale lock recovery occurs only when lock age >1h and PID no longer exists

## Acceptance Predicate Command
```bash
python3 ~/.claude/bin/postflight_acceptance_check.py \
  --run-summary ~/.claude/state/postflight_runs/<task>/<track>/<run>/run_summary.json \  <!-- pointer-check:skip -->
  --out ~/.claude/state/postflight_runs/<task>/<track>/<run>/acceptance_check.json \  <!-- pointer-check:skip -->
  --mode enforce
```

## Troubleshooting
- `REAL_BIN_NOT_FOUND`:
  - set `CLAUDE_HOME` or install/alias `codex.real`.
- `LOCK_BUSY`:
  - wait and retry, or inspect lock metadata under `~/.claude/state/locks/`.
- `ROUTE_CLASS_IMMUTABLE_VIOLATION`:
  - do not reuse a `route_task_id` with a different route class.
- `PREDICATE_FAILED`:
  - inspect `telemetry.log`, `run_summary.json`, and finalize artifacts for branch mismatch.

## Rollback
1. Immediate mitigation:
   - set `postflight.mode` to `audit`.
2. Emergency stop:
   - set `postflight.enabled=false`.
3. Keep telemetry extraction running for forensic visibility.
4. Validate rollback:
   - run one `R3/R4` finalize attempt and confirm wrapper returns underlying codex exit behavior.
