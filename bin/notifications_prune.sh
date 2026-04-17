#!/usr/bin/env bash
set -euo pipefail

CLAUDE_HOME_DIR="${CLAUDE_HOME:-${CODEX_HOME:-${HOME}/.claude}}"
LOG_FILE="$CLAUDE_HOME_DIR/log/notifications.log"
STATE_FILE="$CLAUDE_HOME_DIR/state/notify_state.tsv"
KEEP_LOG_LINES="${1:-2000}"
KEEP_STATE_LINES="${2:-500}"

[[ "$KEEP_LOG_LINES" =~ ^[0-9]+$ ]] || { echo "ERROR: keep log lines must be numeric" >&2; exit 1; }
[[ "$KEEP_STATE_LINES" =~ ^[0-9]+$ ]] || { echo "ERROR: keep state lines must be numeric" >&2; exit 1; }

if [[ -f "$LOG_FILE" ]]; then
  awk -F'\t' 'NF >= 4' "$LOG_FILE" | tail -n "$KEEP_LOG_LINES" > "${LOG_FILE}.tmp"
  mv "${LOG_FILE}.tmp" "$LOG_FILE"
  echo "Pruned and normalized log to $KEEP_LOG_LINES lines: $LOG_FILE"
else
  echo "No log file found: $LOG_FILE"
fi

if [[ -f "$STATE_FILE" ]]; then
  awk -F'\t' 'NF == 2' "$STATE_FILE" | tail -n "$KEEP_STATE_LINES" > "${STATE_FILE}.tmp"
  mv "${STATE_FILE}.tmp" "$STATE_FILE"
  echo "Pruned and normalized state to $KEEP_STATE_LINES lines: $STATE_FILE"
else
  echo "No state file found: $STATE_FILE"
fi
