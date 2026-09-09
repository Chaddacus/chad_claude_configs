"""web_budget.py — single source of truth for the "don't run away with web search" policy.

Two callers share this so the budget/circuit can never drift between them:
  * web_search_breaker.py — PreToolUse/PostToolUse hook on the NATIVE WebSearch/WebFetch tools.
  * web_search.py         — the Bash-callable curl search helper teammates use (they don't
                            get the native tool in experimental-teams mode).

Policy: a per-session-window CALL BUDGET (volume runaway) plus a CONSECUTIVE-FAILURE trip
(the 2026-07-13 bot-wall cascade). Circuit states: closed (normal) / open (deny fast until
cooldown) / half-open (first call after cooldown probes; its outcome closes or re-opens).

State lives in ~/.claude/state/web_search_breaker.json keyed by session id, so a whole
teammate fan-out sharing one session shares one budget — the swarm collectively can't run
away. Env knobs (kill-switch = MAX_CALLS 0): WEB_BREAKER_MAX_CALLS / _WINDOW_S / _MAX_FAILS
/ _COOLDOWN_S. All functions FAIL OPEN on internal error — a bug here must never wedge search.
"""
import fcntl
import json
import os
import time
from contextlib import contextmanager

CLAUDE_HOME = os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))
STATE = os.path.join(CLAUDE_HOME, "state", "web_search_breaker.json")
LOCK = STATE + ".lock"

MAX_CALLS = int(os.environ.get("WEB_BREAKER_MAX_CALLS", "50"))    # per session-window
WINDOW_S = int(os.environ.get("WEB_BREAKER_WINDOW_S", "3600"))     # rolling window (s)
MAX_FAILS = int(os.environ.get("WEB_BREAKER_MAX_FAILS", "5"))      # consecutive fails -> open
COOLDOWN_S = int(os.environ.get("WEB_BREAKER_COOLDOWN_S", "120"))  # open duration (s)


def resolve_sid(explicit=None):
    """Resolve the session key: an explicit id (hook payload) beats the env, which beats a
    fixed default. Both callers land on the same key when Claude sets CLAUDE_CODE_SESSION_ID,
    so the native-tool path and the curl-helper path share one budget bucket."""
    return (
        explicit
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or "default"
    )


@contextmanager
def _locked():
    """Serialize the read-modify-write across concurrent invocations.

    Teammate fan-outs run the breaker hooks in parallel; without this lock,
    simultaneous check() calls both read count=N and both wrote N+1, losing
    increments — the budget undercounted in exactly the swarm scenario it
    guards (RMW race noted when the file was first committed, 2026-07-16).

    FAIL OPEN like everything else here: if the lock can't be acquired within
    ~200ms, proceed unlocked — a lost increment beats a wedged search path.
    """
    fh = None
    try:
        os.makedirs(os.path.dirname(LOCK), exist_ok=True)
        fh = open(LOCK, "w")
        for _ in range(20):
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(0.01)
    except Exception:
        fh = None  # fail open: proceed unlocked
    try:
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


def _load():
    """Load all-session state; missing/corrupt -> empty (fail open)."""
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(state):
    """Atomically persist state; swallow IO errors (never block on bookkeeping)."""
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE)
    except Exception:
        pass


def _session(all_state, sid, now):
    """Return the session record, resetting it when the rolling window elapses."""
    s = all_state.get(sid)
    if not s or now - s.get("window_start", 0) > WINDOW_S:
        s = {"count": 0, "window_start": now, "fails": 0, "opened_until": 0}
        all_state[sid] = s
    return s


def check(sid):
    """Decide whether one web call is allowed for `sid`, counting it when allowed.

    Returns (allowed: bool, reason: str). Fails OPEN on any internal error. Call `record`
    afterward with the call's outcome so the failure-trip stays accurate."""
    try:
        with _locked():
            now = int(time.time())
            st = _load()
            s = _session(st, sid, now)
            if s.get("opened_until", 0) > now:
                wait = s["opened_until"] - now
                return False, (
                    f"web-search circuit OPEN — {s.get('fails', 0)} consecutive failures, "
                    f"cooling down {wait}s. Stop retrying: switch approach, verify a different "
                    f"source, or surface the tool failure to the user (this guard exists because "
                    f"a research fan-out hammered a failing tool on 2026-07-13)."
                )
            if s.get("count", 0) >= MAX_CALLS:
                return False, (
                    f"web-search budget exhausted: {s.get('count', 0)}/{MAX_CALLS} calls this "
                    f"{WINDOW_S // 60}m window. Consolidate and report; raising the cap "
                    f"(WEB_BREAKER_MAX_CALLS) is the user's call, not yours."
                )
            s["count"] = s.get("count", 0) + 1
            _save(st)
            return True, "ok"
    except Exception:
        return True, "ok"  # fail open


def record(sid, ok):
    """Record a web call's outcome: a success resets the streak + closes the circuit; a
    failure increments the streak and opens the circuit after MAX_FAILS consecutive."""
    try:
        with _locked():
            now = int(time.time())
            st = _load()
            s = _session(st, sid, now)
            if ok:
                s["fails"] = 0
                s["opened_until"] = 0
            else:
                s["fails"] = s.get("fails", 0) + 1
                if s["fails"] >= MAX_FAILS:
                    s["opened_until"] = now + COOLDOWN_S
            _save(st)
    except Exception:
        pass  # fail open
