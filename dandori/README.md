# Dandori runtime refactor

Reorganizes the `~/.claude` runtime around one axiom: **the critical path is
sacred — the user waiting is the machine stopped.** Headline metric: *changeover
time* (prompt → first useful action). The reform is the SMED move: convert
internal setup (runs while the user waits) to external setup (pre-staged /
speculative / background), losing no governance.

## Status: additive foundation built + verified (NOT yet cut over)

Everything here is **new and additive**. Nothing in the live runtime reads it,
so it cannot break anything. Each piece ships with a validator proving it is
faithful to the live system, which is the gate that makes a later cutover safe.

| Slice | Artifact | Proof | Result |
|---|---|---|---|
| 1. Unified contract | `dandori.json` (gen by `bin/generate_contract.py`) | `bin/validate_contract.py` | PASS — faithful to route.v2; adds budgets+prep+gates |
| 2. Hook 5S sort | `hooks.json` + `bench/{internal,external}/` | `bin/hooks_audit.py` | PASS — 42/42 classified, 0 drift; 138s convertible |
| 3. Streaming gate | `bin/stream_gates.py` + `stream_verify_project.py` (real project commands, shadow) | `python3 bin/stream_gates.py selftest` | PASS — runs completion_gate's resolved commands; FAIL caught; non-authoritative; stale tree not reused |
| 4. Redundancy sort | (report) | `bin/redundancy_report.py` | 4/4 concerns duplicated; collapse plan emitted |

Re-run all proofs:

```bash
python3 bin/generate_contract.py && python3 bin/validate_contract.py
python3 bin/hooks_audit.py
python3 bin/stream_gates.py selftest
python3 bin/redundancy_report.py
```

## Remaining: the cutover (deliberate, gated, irreversible — not yet done)

These edit the live runtime and trip `policy_edit_gate` / CR-INV-001. Each is a
reversible flip done one at a time, behind the validators above, old files kept
until the new path is proven:

1. **Streaming gate flip.** ✅ SHADOW LIVE (settings.json):
   `hook_record_edit.py` on `PostToolUse(Edit|Write)` feeds the gate;
   `hook_shadow_stop.py` on `Stop` logs the streamed decision to
   `state/dandori/shadow_log.jsonl`. Non-blocking, non-authoritative
   (`config.json` → `streaming_gates: shadow`). Backup:
   `settings.json.bak-dandori-shadow-*`. Revert = set flag `off` or drop the 2
   hook lines.
   - `hook_shadow_stop.py` now reads `completion_gate`'s real verdict (its
     `/tmp/claude-verify-*` ledger, written earlier in the Stop chain) and logs
     **streamed-vs-real agreement** with `FALSE_GREEN` (streamed PASS / real
     FAIL) as the hard safety class. `stream_gates` verifies with the project's
     resolved commands (same as completion_gate), so the comparison is
     apples-to-apples. Dashboard: `bin/shadow_report.py`.
   - **Remaining before `"on"`:** accumulate real-session samples; flip only when
     `shadow_report` shows **0 false-greens**, >=20 decisive samples, and a low
     INCONCLUSIVE rate. KNOWN TRADEOFF the data will quantify: streaming runs the
     project's suite in the background per edit (bounded to 1-in-flight-per-root
     by supersede). For slow suites this means high INCONCLUSIVE + overhead —
     streaming likely pays off for fast checks (typecheck/lint), not slow E2E.
   - KNOWN DEBT: `find_project_root`/`resolve_commands`/`run_command` are mirrored
     from `completion_gate.py` (it `sys.exit`s at import). Consolidate into one
     shared module at cutover.
2. **Speculative classify.** Move `classify_prompt.py` to pre-run on partial
   input; at submit only confirm. Removes 10s.
3. **Contract cutover.** Repoint readers of `route_manifest.json` route rules at
   `dandori.json`; keep the manifest generated/validated from it. Gated by
   `validate_contract.py` staying green.
4. **Redundancy collapse.** Per `redundancy_report.py`: make each concern's
   canonical home authoritative; replace the other expressions with one-line
   references. Requires editing CLAUDE.md (explicit user authorization).

## Honest scope note

This foundation proves faithfulness and de-risks the cutover; it does **not**
itself change runtime behavior. The behavior change happens at step 1's flip,
measured on live sessions before it becomes authoritative.
