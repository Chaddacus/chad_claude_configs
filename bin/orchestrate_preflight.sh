#!/bin/bash
# Preflight check for /orchestrate-local. Exits 0 if wiring is healthy, nonzero otherwise.
# Output is human-readable; caller scrapes exit code for decision.
#
# Mode is selected via CW_GOOSE_MODE:
#   local    (default) — check LM Studio is reachable + model loaded + api key set
#   provider — check goose's config.yaml exists and is NOT pointed at localhost, and
#              that at least one provider API key env var is present
#
# Override the expected local model name via CW_GOOSE_MODEL (default: daily-heavy).
# Override the local endpoint via CW_GOOSE_ENDPOINT (default: http://localhost:1234).

set -u
failures=0
MODE="${CW_GOOSE_MODE:-local}"
MODEL_NAME="${CW_GOOSE_MODEL:-daily-heavy}"
ENDPOINT="${CW_GOOSE_ENDPOINT:-http://localhost:1234}"

check() {
  local label="$1" ; shift
  if "$@" >/dev/null 2>&1; then
    printf "  OK   %s\n" "$label"
  else
    printf "  FAIL %s\n" "$label"
    failures=$((failures + 1))
  fi
}

echo "orchestrate-local preflight (mode=$MODE):"

# Mode-independent checks
check "goose binary exists" bash -c 'command -v goose'
check "dispatcher exists" test -x "$HOME/.claude/bin/goose_dispatch.py"
check ".goosehints present" test -s "$HOME/.goosehints"
check "skills dir populated" test -d "$HOME/.config/goose/skills"
check "goose config.yaml present" test -f "$HOME/.config/goose/config.yaml"

if [ "$MODE" = "local" ]; then
  # Local LM Studio checks
  check "LM Studio reachable at $ENDPOINT" curl -s --max-time 3 "$ENDPOINT/v1/models"
  check "model '$MODEL_NAME' loaded" bash -c "
    curl -s --max-time 3 '$ENDPOINT/v1/models' \
    | python3 -c \"import sys,json; d=json.load(sys.stdin); sys.exit(0 if any(m['id']=='$MODEL_NAME' for m in d['data']) else 1)\"
  "
  check "LM_STUDIO_API_KEY set" bash -c 'test -n "${LM_STUDIO_API_KEY:-}"'
elif [ "$MODE" = "provider" ]; then
  # Provider-mode checks: verify an effective non-local provider and that SOME
  # form of provider credential is available in the environment.
  #
  # Effective provider = env GOOSE_PROVIDER if set, else whatever config.yaml
  # declares. This lets a per-project wrapper override a global config.yaml
  # that points at a local backend (other sessions may depend on the global).
  EFFECTIVE_PROVIDER="${GOOSE_PROVIDER:-}"
  if [ -z "$EFFECTIVE_PROVIDER" ] && [ -f "$HOME/.config/goose/config.yaml" ]; then
    EFFECTIVE_PROVIDER="$(grep -E '^[[:space:]]*GOOSE_PROVIDER:' "$HOME/.config/goose/config.yaml" \
      | head -1 | sed -E 's/^[[:space:]]*GOOSE_PROVIDER:[[:space:]]*//; s/[[:space:]]*$//')"
  fi
  check "effective provider is non-local (GOOSE_PROVIDER=$EFFECTIVE_PROVIDER)" bash -c "
    case \"$EFFECTIVE_PROVIDER\" in
      lmstudio|ollama|llama.cpp|llamacpp|'' ) exit 1 ;;
      * ) exit 0 ;;
    esac
  "
  check "at least one provider API key env var set" bash -c '
    test -n "${OPENAI_API_KEY:-}${ANTHROPIC_API_KEY:-}${GOOGLE_API_KEY:-}${GEMINI_API_KEY:-}${GROQ_API_KEY:-}${OPENROUTER_API_KEY:-}${DATABRICKS_TOKEN:-}${AZURE_OPENAI_API_KEY:-}"
  '
else
  printf "  FAIL unknown CW_GOOSE_MODE='%s' (expected 'local' or 'provider')\n" "$MODE"
  failures=$((failures + 1))
fi

if (( failures > 0 )); then
  echo ""
  echo "$failures check(s) failed. Do not dispatch."
  exit 1
fi

echo ""
echo "preflight passed."
exit 0
