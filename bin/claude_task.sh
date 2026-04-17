#!/usr/bin/env bash
set -euo pipefail

CLAUDE_HOME_DIR="${CLAUDE_HOME:-${HOME}/.claude}"
STATE_DIR="$CLAUDE_HOME_DIR/state"
TASK_STATE_FILE="$STATE_DIR/current_task.env"
NOTIFY_SCRIPT="$CLAUDE_HOME_DIR/bin/notify_done.sh"

usage() {
  cat <<'EOF'
Usage:
  claude_task.sh <command> [options]

Commands:
  start --task <text>
  done [--status <success|failure>] [--details <text>] [--channel <auto|sms|whatsapp|desktop>] [--env-file <path>] [--force] [--dry-run]
  status
  clear
EOF
}

cmd="${1:-}"
[[ -n "$cmd" ]] || { usage >&2; exit 1; }
shift || true

mkdir -p "$STATE_DIR"

case "$cmd" in
  start)
    task=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --task)
          task="${2:-}"
          shift 2
          ;;
        *)
          echo "ERROR: unknown start option: $1" >&2
          exit 1
          ;;
      esac
    done

    [[ -n "$task" ]] || { echo "ERROR: --task is required for start" >&2; exit 1; }
    now="$(date +%s)"
    {
      printf 'TASK_NAME=%q\n' "$task"
      printf 'TASK_STARTED_AT=%q\n' "$now"
      printf 'TASK_CWD=%q\n' "$PWD"
    } > "$TASK_STATE_FILE"
    echo "Started task: $task"
    ;;

  done)
    [[ -x "$NOTIFY_SCRIPT" ]] || { echo "ERROR: notify script missing: $NOTIFY_SCRIPT" >&2; exit 1; }
    [[ -f "$TASK_STATE_FILE" ]] || { echo "ERROR: no active task. Run 'claude_task.sh start --task ...' first." >&2; exit 1; }

    status="success"
    details=""
    channel="auto"
    env_file_path=""
    force=false
    dry_run=false

    while [[ $# -gt 0 ]]; do
      case "$1" in
        --status)
          status="${2:-}"
          shift 2
          ;;
        --details)
          details="${2:-}"
          shift 2
          ;;
        --channel)
          channel="${2:-}"
          shift 2
          ;;
        --env-file)
          env_file_path="${2:-}"
          shift 2
          ;;
        --force)
          force=true
          shift
          ;;
        --dry-run)
          dry_run=true
          shift
          ;;
        *)
          echo "ERROR: unknown done option: $1" >&2
          exit 1
          ;;
      esac
    done

    # shellcheck disable=SC1090
    source "$TASK_STATE_FILE"

    extra=""
    if [[ -n "${TASK_CWD:-}" && "$TASK_CWD" != "$PWD" ]]; then
      extra="(started_in:${TASK_CWD})"
    fi

    merged_details="$details"
    if [[ -n "$extra" ]]; then
      if [[ -n "$merged_details" ]]; then
        merged_details="$merged_details $extra"
      else
        merged_details="$extra"
      fi
    fi

    notify_cmd=(
      "$NOTIFY_SCRIPT"
      --status "$status"
      --task "${TASK_NAME:-task}"
      --details "$merged_details"
      --started-at "${TASK_STARTED_AT:-}"
      --channel "$channel"
    )

    [[ -n "$env_file_path" ]] && notify_cmd+=(--env-file "$env_file_path")
    [[ "$force" == true ]] && notify_cmd+=(--force)
    [[ "$dry_run" == true ]] && notify_cmd+=(--dry-run)

    "${notify_cmd[@]}"
    if [[ "$dry_run" != true ]]; then
      rm -f "$TASK_STATE_FILE"
    fi
    ;;

  status)
    if [[ -f "$TASK_STATE_FILE" ]]; then
      cat "$TASK_STATE_FILE"
    else
      echo "No active task"
    fi
    ;;

  clear)
    rm -f "$TASK_STATE_FILE"
    echo "Cleared active task state"
    ;;

  *)
    usage >&2
    exit 1
    ;;
esac
