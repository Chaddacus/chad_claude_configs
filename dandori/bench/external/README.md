# bench/external — soto-dandori (外段取り)

Hooks here run **off the critical path**: pre-staged at session start, run
speculatively, or executed in the background while the turn proceeds. The user
never waits on them. This is where most work belongs.

34 of 42 live hooks are already external (telemetry, logging, recovery,
session-start staging, post-tool recording). The session-start group
(`omni_mem_*`, `rlm_session_preflight`) is the canonical soto-dandori: it stages
memory, briefing, inbox, and codebase index *before* the first prompt so the
changeover is cheap.

The refactor's goal is to grow this set by converting the convertible internal
hooks listed in `../internal/README.md` — starting with `completion_gate`
(streaming, proven in `experiments/streaming-gates`, promoted to
`bin/stream_gates.py`).
