#!/usr/bin/env python3
"""slice_retry.py — CP7 of the autonomous outer-loop driver: retry / sentinel.

Wraps a single slice's execution (CP5 `execute_slice`) with a respawn policy.
Every attempt is a FRESH worker in a FRESH worktree — the point of the whole
design is that a derailed or context-exhausted worker is cheaply discarded and
retried with a clean context, mirroring goose_dispatch's `{slice_id}-try{N}`
pattern. CP7 decides, per failure, whether to respawn, back off (rate limit),
or give up:

- transient stages (worker / empty_diff / verifier_subprocess): a fresh respawn
  usually fixes a one-off derail -> retry up to `max_attempts`.
- hard stages (static_gate / verify / apply / candidate_* / snapshot): the WORK
  is wrong, not the session -> retry at most `hard_stage_max_attempts` (with the
  failure reason fed back into the next prompt), then give up so budget isn't
  burned respawning the same mistake.
- rate-limit signal in worker output: sleep and retry WITHOUT consuming a normal
  attempt, capped by `max_rate_limit_retries`.

CP7 owns no track state and no plan knowledge — CP6 (outer_loop_driver) supplies
a `build_spec(attempt, last_result)` callback and records the verdict.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).parent))

from rate_limit_guard import detect_rate_limit_signal
from slice_executor import ExecutorResult, SliceSpec, execute_slice

# Failure stages where a fresh respawn is worth trying (the session derailed).
TRANSIENT_STAGES = frozenset({"worker", "empty_diff", "verifier_subprocess"})
# Failure stages where the produced work is wrong, not the session — escalate
# faster. (snapshot/candidate_* are infra failures; treated as hard so we don't
# loop forever on a broken environment.)
HARD_STAGES = frozenset({
    "static_gate", "verify", "apply",
    "candidate_setup", "candidate_apply", "snapshot",
})

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_HARD_STAGE_MAX_ATTEMPTS = 2
DEFAULT_MAX_RATE_LIMIT_RETRIES = 3
DEFAULT_RATE_LIMIT_BACKOFF_S = 300


@dataclass
class RetryOutcome:
    ok: bool
    attempts: int                       # real (non-rate-limited) executions
    rate_limit_retries: int
    final_result: Optional[ExecutorResult]
    gave_up_reason: Optional[str] = None  # None on success
    events: list[dict] = field(default_factory=list)


def _combined_output(res: ExecutorResult) -> str:
    """Join every stream we might see a quota signal in."""
    parts: list[str] = []
    wr = res.worker_result
    if wr is not None:
        parts.append(getattr(wr, "stdout", "") or "")
        parts.append(getattr(wr, "stderr", "") or "")
    parts.append(res.verifier_stdout or "")
    parts.append(res.verifier_stderr or "")
    parts.append(res.error or "")
    return "\n".join(parts)


def run_slice_with_retry(
    *,
    main_repo: Path,
    build_spec: Callable[[int, Optional[ExecutorResult]], SliceSpec],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    hard_stage_max_attempts: int = DEFAULT_HARD_STAGE_MAX_ATTEMPTS,
    max_rate_limit_retries: int = DEFAULT_MAX_RATE_LIMIT_RETRIES,
    rate_limit_backoff_s: int = DEFAULT_RATE_LIMIT_BACKOFF_S,
    execute_fn: Callable[..., ExecutorResult] = execute_slice,
    sleep: Callable[[float], None] = time.sleep,
    on_event: Optional[Callable[[dict], None]] = None,
) -> RetryOutcome:
    """Execute one slice with fresh-respawn retries. See module docstring.

    `build_spec(attempt, last_result)` is called before every execution so CP6
    can rotate the branch name and fold the previous failure into the prompt.
    `execute_fn` and `sleep` are injectable for testing.
    """
    events: list[dict] = []

    def emit(ev: dict) -> None:
        events.append(ev)
        if on_event is not None:
            on_event(ev)

    attempts = 0
    rate_limit_retries = 0
    last: Optional[ExecutorResult] = None

    while True:
        spec = build_spec(attempts + 1, last)
        res = execute_fn(main_repo=main_repo, spec=spec)
        last = res

        if res.ok:
            emit({"event": "slice_accepted", "attempt": attempts + 1, "stage": res.stage,
                  "new_head_sha": res.new_head_sha})
            return RetryOutcome(
                ok=True, attempts=attempts + 1, rate_limit_retries=rate_limit_retries,
                final_result=res, events=events,
            )

        # Rate-limit: back off and retry without consuming a normal attempt.
        if detect_rate_limit_signal(_combined_output(res)):
            rate_limit_retries += 1
            emit({"event": "rate_limited", "rate_limit_retries": rate_limit_retries,
                  "backoff_s": rate_limit_backoff_s})
            if rate_limit_retries > max_rate_limit_retries:
                return RetryOutcome(
                    ok=False, attempts=attempts, rate_limit_retries=rate_limit_retries,
                    final_result=res, gave_up_reason="rate_limit_exhausted", events=events,
                )
            sleep(rate_limit_backoff_s)
            continue

        attempts += 1
        is_hard = res.stage in HARD_STAGES
        cap = hard_stage_max_attempts if is_hard else max_attempts
        emit({"event": "slice_attempt_failed", "attempt": attempts, "stage": res.stage,
              "hard": is_hard, "cap": cap, "error": (res.error or "")[:200]})

        if attempts >= cap:
            reason = "hard_stage_limit" if is_hard else "max_attempts"
            return RetryOutcome(
                ok=False, attempts=attempts, rate_limit_retries=rate_limit_retries,
                final_result=res, gave_up_reason=reason, events=events,
            )
        # else: loop; build_spec(attempts+1, last) will augment the next prompt.
