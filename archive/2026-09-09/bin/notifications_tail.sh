#!/usr/bin/env bash
set -euo pipefail

CLAUDE_HOME_DIR="${CLAUDE_HOME:-${CODEX_HOME:-${HOME}/.claude}}"
LOG_FILE="$CLAUDE_HOME_DIR/log/notifications.log"
LINES=40
FOLLOW=false
ERRORS_ONLY=false

usage() {
  cat <<'EOF'
Usage:
  notifications_tail.sh [options]

Options:
  -n <lines>       Number of lines (default: 40)
  --follow         Follow log output
  --errors-only    Show only error events
  -h, --help       Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n)
      LINES="${2:-}"
      shift 2
      ;;
    --follow)
      FOLLOW=true
      shift
      ;;
    --errors-only)
      ERRORS_ONLY=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

[[ "$LINES" =~ ^[0-9]+$ ]] || { echo "ERROR: -n must be a non-negative integer" >&2; exit 1; }
[[ -f "$LOG_FILE" ]] || { echo "No notification log file at $LOG_FILE"; exit 0; }

render() {
  if [[ "$ERRORS_ONLY" == true ]]; then
    grep $'\terror\t' || true
  else
    cat
  fi
}

if [[ "$FOLLOW" == true ]]; then
  tail -n "$LINES" -f "$LOG_FILE" | render
else
  tail -n "$LINES" "$LOG_FILE" | render
fi
