#!/bin/bash
# Preflight check for /orchestrate-local. Exits 0 if wiring is healthy, nonzero otherwise.
# Output is human-readable; caller scrapes exit code for decision.

set -u
failures=0

check() {
  local label="$1" ; shift
  if "$@" >/dev/null 2>&1; then
    printf "  OK   %s\n" "$label"
  else
    printf "  FAIL %s\n" "$label"
    failures=$((failures + 1))
  fi
}

echo "orchestrate-local preflight:"

check "goose binary exists" test -x /opt/homebrew/bin/goose
check "dispatcher exists" test -x "$HOME/.claude/bin/goose_dispatch.py"
check ".goosehints present" test -s "$HOME/.goosehints"
check "skills dir populated" test -d "$HOME/.config/goose/skills"
check "LM Studio reachable on localhost:1234" curl -s --max-time 3 http://localhost:1234/v1/models
check "daily-heavy model loaded" bash -c '
  curl -s --max-time 3 http://localhost:1234/v1/models \
  | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if any(m[\"id\"]==\"daily-heavy\" for m in d[\"data\"]) else 1)"
'
check "LM_STUDIO_API_KEY set" bash -c 'test -n "${LM_STUDIO_API_KEY:-}"'

if (( failures > 0 )); then
  echo ""
  echo "$failures check(s) failed. Do not dispatch."
  exit 1
fi

echo ""
echo "preflight passed."
exit 0
