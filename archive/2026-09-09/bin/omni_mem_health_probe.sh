#!/bin/bash
# omni-mem health probe — fast SessionStart check for the memory daemon.
#
# Silent if healthy (200 OK from /health). Emits a loud stderr warning
# if unreachable or non-200, so a dead daemon is noticed immediately
# rather than after a session of silent memory-save failures.
#
# Budget: ~100ms typical. Never blocks startup longer than 2s.

set +e

ENDPOINT="${OMNI_MEM_HEALTH_URL:-http://localhost:8765/health}"

code=$(/usr/bin/curl -sS -o /dev/null -w "%{http_code}" --max-time 2 "$ENDPOINT" 2>/dev/null)
rc=$?

if [ "$rc" -ne 0 ]; then
    echo "[omni-mem] WARN: health probe to $ENDPOINT failed (curl rc=$rc) — memory saves will silently fail this session. Check Docker container." >&2
    exit 0
fi

if [ "$code" != "200" ]; then
    echo "[omni-mem] WARN: health probe returned HTTP $code (expected 200) — memory saves may be degraded." >&2
    exit 0
fi

# Healthy — silent.
exit 0
