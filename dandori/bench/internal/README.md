# bench/internal — uchi-dandori (内段取り)

Hooks here run **while the user/turn is blocked**. This is the critical path —
every second spent here is the user waiting. Keep this set as small as possible.

A new hook lands here ONLY if it must decide synchronously before the agent can
proceed. The default for any new hook is `bench/external`.

## Current internal hooks (from `hooks_audit.py`)

**Irreducible — must stay synchronous:**
- `stop_gate.py` (Stop) — lexical stop guard; decides whether the stop is allowed
- `idle_timing_hook.py` (UserPromptSubmit) — augments the prompt; cheap
- `pre_tool_guard.py` (PreToolUse) — destructive-command guard
- `policy_edit_gate.py` (PreToolUse) — must gate before an edit applies

**Convertible — the SMED worklist (move these to external):**
- `completion_gate.py` (Stop, 60s) and (TaskCompleted, 60s) — stream during execution
- `classify_prompt.py` (UserPromptSubmit, 10s) — pre-classify speculatively
- `replan_evidence_check.py` (Stop, 8s) — stream during the turn

Convertible internal budget: **138s** of off-loadable critical-path time.
