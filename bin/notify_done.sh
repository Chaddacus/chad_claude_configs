#!/usr/bin/env bash
set -euo pipefail

STATUS="success"
TASK=""
DETAILS=""
BODY=""
DRY_RUN=false
CHANNEL="auto"
ENV_FILE=""
URGENT=false
FORCE=false
STARTED_AT=""

CLAUDE_HOME_DIR="${CLAUDE_HOME:-${CODEX_HOME:-${HOME}/.claude}}"
SMS_ENV_FILE_DEFAULT="${HOME}/.config/codex/secrets/twilio.env"
WHATSAPP_ENV_FILE_DEFAULT="${HOME}/.config/codex/secrets/whatsapp.env"
TELEGRAM_ENV_FILE_DEFAULT="${HOME}/.claude/secrets/telegram.env"
CHECK_SCRIPT="$CLAUDE_HOME_DIR/bin/check_notifications_env.sh"
SMS_SCRIPT="$CLAUDE_HOME_DIR/skills/twilio-completion-sms/scripts/send_sms.py"
WHATSAPP_SCRIPT="$CLAUDE_HOME_DIR/skills/whatsapp-completion/scripts/send_whatsapp.py"
STATE_DIR="$CLAUDE_HOME_DIR/state"
STATE_FILE="$STATE_DIR/notify_state.tsv"
LOG_DIR="$CLAUDE_HOME_DIR/log"
LOG_FILE="$LOG_DIR/notifications.log"

NOTIFY_QUIET_HOURS="${NOTIFY_QUIET_HOURS:-}"
NOTIFY_DEDUPE_WINDOW_SECONDS="${NOTIFY_DEDUPE_WINDOW_SECONDS:-0}"
NOTIFY_FAILURE_BYPASS_QUIET="${NOTIFY_FAILURE_BYPASS_QUIET:-true}"
NOTIFY_DESKTOP_SOUND="${NOTIFY_DESKTOP_SOUND:-}"
NOTIFY_DESKTOP_SOUND_DISABLED="${NOTIFY_DESKTOP_SOUND_DISABLED:-false}"
NOTIFY_DESKTOP_SOUND_DURATION_SECONDS="${NOTIFY_DESKTOP_SOUND_DURATION_SECONDS:-5}"
NOTIFY_DESKTOP_SOUND_COOLDOWN_SECONDS="${NOTIFY_DESKTOP_SOUND_COOLDOWN_SECONDS:-0}"
NOTIFY_DESKTOP_SYSTEM_CHIME="${NOTIFY_DESKTOP_SYSTEM_CHIME:-true}"
DEFAULT_DESKTOP_SOUND_SUCCESS="Glass"
DEFAULT_DESKTOP_SOUND_FAILURE="Basso"
if [[ -f "${CLAUDE_HOME_DIR}/sounds/ffx-victory.mp3" ]]; then
  DEFAULT_DESKTOP_SOUND_SUCCESS="${CLAUDE_HOME_DIR}/sounds/ffx-victory.mp3"
elif [[ -f "${HOME}/.codex/sounds/ffx-victory.mp3" ]]; then
  DEFAULT_DESKTOP_SOUND_SUCCESS="${HOME}/.codex/sounds/ffx-victory.mp3"
fi
if [[ -f "${CLAUDE_HOME_DIR}/sounds/game-over.mp3" ]]; then
  DEFAULT_DESKTOP_SOUND_FAILURE="${CLAUDE_HOME_DIR}/sounds/game-over.mp3"
elif [[ -f "${HOME}/.codex/sounds/game-over.mp3" ]]; then
  DEFAULT_DESKTOP_SOUND_FAILURE="${HOME}/.codex/sounds/game-over.mp3"
fi
NOTIFY_DESKTOP_SOUND_SUCCESS="${NOTIFY_DESKTOP_SOUND_SUCCESS:-$DEFAULT_DESKTOP_SOUND_SUCCESS}"
NOTIFY_DESKTOP_SOUND_FAILURE="${NOTIFY_DESKTOP_SOUND_FAILURE:-$DEFAULT_DESKTOP_SOUND_FAILURE}"

usage() {
  cat <<'EOF'
Usage:
  notify_done.sh [options]

Options:
  --status <success|failure>     Completion status (default: success)
  --task <text>                  Short task name
  --details <text>               Short completion detail
  --body <text>                  Explicit message body (overrides generated text)
  --channel <auto|sms|whatsapp|desktop|telegram>
                                 Channel selection (default: auto)
  --env-file <path>              Env file override for explicit sms/whatsapp channel
  --started-at <epoch-seconds>   Start time used to compute elapsed duration
  --urgent                        Bypass quiet-hours suppression
  --force                         Bypass duplicate suppression
  --dry-run                       Validate and print payload without sending
  -h, --help                      Show this help

Environment:
  NOTIFY_QUIET_HOURS             Quiet hours range, e.g. 23:00-08:00
  NOTIFY_DEDUPE_WINDOW_SECONDS   Duplicate suppression window (default: 0; set >0 to enable)
  NOTIFY_FAILURE_BYPASS_QUIET    true/false (default: true)
  NOTIFY_DESKTOP_SOUND           Override desktop sound for all statuses (name or absolute path)
  NOTIFY_DESKTOP_SOUND_SUCCESS   Sound name/path for success desktop notifications
  NOTIFY_DESKTOP_SOUND_FAILURE   Sound name/path for failure desktop notifications
  NOTIFY_DESKTOP_SOUND_DISABLED  true/false to disable desktop sound (default: false)
  NOTIFY_DESKTOP_SOUND_DURATION_SECONDS
                                 Playback duration in seconds (default: 5)
  NOTIFY_DESKTOP_SOUND_COOLDOWN_SECONDS
                                 Minimum seconds between desktop sounds (default: 0)
  NOTIFY_DESKTOP_SYSTEM_CHIME    true/false; play macOS notification chime (default: true)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --status)
      STATUS="${2:-}"
      shift 2
      ;;
    --task)
      TASK="${2:-}"
      shift 2
      ;;
    --details)
      DETAILS="${2:-}"
      shift 2
      ;;
    --body)
      BODY="${2:-}"
      shift 2
      ;;
    --channel)
      CHANNEL="${2:-}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --started-at)
      STARTED_AT="${2:-}"
      shift 2
      ;;
    --urgent)
      URGENT=true
      shift
      ;;
    --force)
      FORCE=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
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

if [[ "$STATUS" != "success" && "$STATUS" != "failure" ]]; then
  echo "ERROR: --status must be success or failure" >&2
  exit 1
fi

if [[ "$CHANNEL" != "auto" && "$CHANNEL" != "sms" && "$CHANNEL" != "whatsapp" && "$CHANNEL" != "desktop" && "$CHANNEL" != "telegram" ]]; then
  echo "ERROR: --channel must be auto, sms, whatsapp, desktop, or telegram" >&2
  exit 1
fi

if [[ ! "$NOTIFY_DEDUPE_WINDOW_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "ERROR: NOTIFY_DEDUPE_WINDOW_SECONDS must be a non-negative integer." >&2
  exit 1
fi

if [[ ! "$NOTIFY_DESKTOP_SOUND_DURATION_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "ERROR: NOTIFY_DESKTOP_SOUND_DURATION_SECONDS must be a non-negative number." >&2
  exit 1
fi

if [[ ! "$NOTIFY_DESKTOP_SOUND_COOLDOWN_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "ERROR: NOTIFY_DESKTOP_SOUND_COOLDOWN_SECONDS must be a non-negative integer." >&2
  exit 1
fi

mkdir -p "$STATE_DIR" "$LOG_DIR"

log_line() {
  local event="$1"
  local channel="$2"
  local note="$3"
  printf '%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$event" "$channel" "$note" >> "$LOG_FILE"
}

is_true() {
  local value="${1:-}"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "TRUE" || "$value" == "yes" || "$value" == "YES" ]]
}

desktop_sound_target() {
  if [[ -n "$NOTIFY_DESKTOP_SOUND" ]]; then
    echo "$NOTIFY_DESKTOP_SOUND"
    return 0
  fi

  if [[ "$STATUS" == "failure" ]]; then
    echo "$NOTIFY_DESKTOP_SOUND_FAILURE"
    return 0
  fi

  echo "$NOTIFY_DESKTOP_SOUND_SUCCESS"
}

play_desktop_sound() {
  if is_true "$NOTIFY_DESKTOP_SOUND_DISABLED"; then
    log_line "sound_skip" "desktop" "disabled"
    return 1
  fi

  local target sound_path now last_ts
  target="$(desktop_sound_target)"
  if [[ -z "$target" ]]; then
    log_line "sound_skip" "desktop" "empty_target"
    return 1
  fi

  now="$(date +%s)"
  if [[ "$NOTIFY_DESKTOP_SOUND_COOLDOWN_SECONDS" -gt 0 && -f "$STATE_DIR/desktop_sound_last_epoch" ]]; then
    last_ts="$(cat "$STATE_DIR/desktop_sound_last_epoch" 2>/dev/null || true)"
    if [[ "$last_ts" =~ ^[0-9]+$ ]] && (( now - last_ts < NOTIFY_DESKTOP_SOUND_COOLDOWN_SECONDS )); then
      log_line "sound_skip" "desktop" "cooldown"
      return 1
    fi
  fi

  if command -v afplay >/dev/null 2>&1; then
    sound_path="$target"
    if [[ "$target" != */* ]]; then
      if [[ -f "/System/Library/Sounds/${target}.aiff" ]]; then
        sound_path="/System/Library/Sounds/${target}.aiff"
      elif [[ -f "/System/Library/Sounds/${target}.caf" ]]; then
        sound_path="/System/Library/Sounds/${target}.caf"
      fi
    fi

    if [[ -f "$sound_path" ]]; then
      if afplay -t "$NOTIFY_DESKTOP_SOUND_DURATION_SECONDS" "$sound_path" >/dev/null 2>&1; then
        echo "$now" > "$STATE_DIR/desktop_sound_last_epoch"
        log_line "sound" "desktop" "custom_played file=$sound_path"
        return 0
      fi
      log_line "sound_error" "desktop" "custom_play_failed file=$sound_path"
      return 1
    fi
    log_line "sound_skip" "desktop" "missing_file $sound_path"
    return 1
  fi

  if command -v osascript >/dev/null 2>&1; then
    osascript -e 'beep 1' >/dev/null 2>&1 || true
    log_line "sound" "desktop" "fallback_beep"
    return 0
  fi

  log_line "sound_error" "desktop" "no_audio_backend"
  return 1
}

is_quiet_time() {
  local range="$1"
  [[ -z "$range" ]] && return 1
  [[ ! "$range" =~ ^([0-9]{2}):([0-9]{2})-([0-9]{2}):([0-9]{2})$ ]] && return 1

  local sh="${BASH_REMATCH[1]}"
  local sm="${BASH_REMATCH[2]}"
  local eh="${BASH_REMATCH[3]}"
  local em="${BASH_REMATCH[4]}"

  local now_min
  now_min=$((10#$(date +%H) * 60 + 10#$(date +%M)))
  local start_min=$((10#$sh * 60 + 10#$sm))
  local end_min=$((10#$eh * 60 + 10#$em))

  if (( start_min < end_min )); then
    (( now_min >= start_min && now_min < end_min ))
  elif (( start_min > end_min )); then
    (( now_min >= start_min || now_min < end_min ))
  else
    return 0
  fi
}

safe_git_branch() {
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git rev-parse --abbrev-ref HEAD 2>/dev/null || true
  fi
}

compute_elapsed() {
  local start="$1"
  [[ -z "$start" ]] && return 0
  [[ ! "$start" =~ ^[0-9]+$ ]] && return 0
  local now
  now="$(date +%s)"
  if (( now >= start )); then
    echo $((now - start))
  fi
}

append_context() {
  local base_details="$1"
  local started_at="$2"
  local project branch elapsed extra

  project="$(basename "$PWD")"
  branch="$(safe_git_branch)"
  elapsed="$(compute_elapsed "$started_at")"

  extra=""
  [[ -n "$project" ]] && extra="project:$project"
  [[ -n "$branch" ]] && extra="${extra:+$extra, }branch:$branch"
  [[ -n "$elapsed" ]] && extra="${extra:+$extra, }elapsed:${elapsed}s"

  if [[ -n "$extra" ]]; then
    if [[ -n "$base_details" ]]; then
      echo "$base_details | $extra"
    else
      echo "$extra"
    fi
  else
    echo "$base_details"
  fi
}

hash_text() {
  if command -v shasum >/dev/null 2>&1; then
    printf '%s' "$1" | shasum -a 256 | awk '{print $1}'
  else
    printf '%s' "$1" | sha256sum | awk '{print $1}'
  fi
}

is_duplicate() {
  local hash="$1"
  local now window ts seen_hash
  now="$(date +%s)"
  window="$NOTIFY_DEDUPE_WINDOW_SECONDS"

  [[ -f "$STATE_FILE" ]] || return 1

  while IFS=$'\t' read -r ts seen_hash; do
    [[ -z "$ts" || -z "$seen_hash" ]] && continue
    [[ ! "$ts" =~ ^[0-9]+$ ]] && continue
    if [[ "$seen_hash" == "$hash" ]] && (( now - ts < window )); then
      return 0
    fi
  done < "$STATE_FILE"

  return 1
}

record_hash() {
  local hash="$1"
  local now
  now="$(date +%s)"
  {
    printf '%s\t%s\n' "$now" "$hash"
    [[ -f "$STATE_FILE" ]] && tail -n 199 "$STATE_FILE"
  } > "${STATE_FILE}.tmp"
  mv "${STATE_FILE}.tmp" "$STATE_FILE"
}

json_wrap() {
  local channel="$1"
  local payload="$2"
  python3 - <<'PY' "$channel" "$payload"
import json
import sys

channel = sys.argv[1]
payload = sys.argv[2]
try:
    parsed = json.loads(payload)
except Exception:
    parsed = {"raw": payload}
print(json.dumps({"channel": channel, "result": parsed}, indent=2))
PY
}

send_sms() {
  local details="$1"
  local env_file
  env_file="$SMS_ENV_FILE_DEFAULT"
  [[ -n "$ENV_FILE" ]] && env_file="$ENV_FILE"

  [[ -x "$CHECK_SCRIPT" ]] || { echo "missing check script: $CHECK_SCRIPT" >&2; return 1; }
  [[ -f "$SMS_SCRIPT" ]] || { echo "missing sms script: $SMS_SCRIPT" >&2; return 1; }

  "$CHECK_SCRIPT" --channel sms --env-file "$env_file" >/dev/null

  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a

  local cmd=(python3 "$SMS_SCRIPT" --status "$STATUS")
  [[ -n "$TASK" ]] && cmd+=(--task "$TASK")
  [[ -n "$details" ]] && cmd+=(--details "$details")
  [[ -n "$BODY" ]] && cmd+=(--body "$BODY")
  [[ "$DRY_RUN" == true ]] && cmd+=(--dry-run)

  "${cmd[@]}"
}

send_whatsapp() {
  local details="$1"
  local env_file
  env_file="$WHATSAPP_ENV_FILE_DEFAULT"
  [[ -n "$ENV_FILE" ]] && env_file="$ENV_FILE"

  [[ -x "$CHECK_SCRIPT" ]] || { echo "missing check script: $CHECK_SCRIPT" >&2; return 1; }
  [[ -f "$WHATSAPP_SCRIPT" ]] || { echo "missing whatsapp script: $WHATSAPP_SCRIPT" >&2; return 1; }

  "$CHECK_SCRIPT" --channel whatsapp --env-file "$env_file" >/dev/null

  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a

  local cmd=(python3 "$WHATSAPP_SCRIPT" --status "$STATUS")
  [[ -n "$TASK" ]] && cmd+=(--task "$TASK")
  [[ -n "$details" ]] && cmd+=(--details "$details")
  [[ -n "$BODY" ]] && cmd+=(--body "$BODY")
  [[ "$DRY_RUN" == true ]] && cmd+=(--dry-run)

  "${cmd[@]}"
}

send_desktop() {
  local details="$1"
  local msg
  if [[ -n "$BODY" ]]; then
    msg="$BODY"
  else
    msg="$STATUS"
    [[ -n "$TASK" ]] && msg="$msg | $TASK"
    [[ -n "$details" ]] && msg="$msg | $details"
  fi

  if [[ "$DRY_RUN" == true ]]; then
    python3 - <<'PY' "$msg"
import json
import sys

print(json.dumps({"mode": "dry-run", "message": sys.argv[1]}))
PY
    return 0
  fi

  if command -v terminal-notifier >/dev/null 2>&1; then
    terminal-notifier -title "Codex Task" -message "$msg"
    if ! play_desktop_sound && is_true "$NOTIFY_DESKTOP_SYSTEM_CHIME"; then
      terminal-notifier -title "Codex Task" -message "$msg" -sound default
    fi
    echo "{\"message\":\"desktop notification sent\"}"
    return 0
  fi

  if command -v osascript >/dev/null 2>&1; then
    osascript - "$msg" <<'APPLESCRIPT'
on run argv
  display notification (item 1 of argv) with title "Codex Task"
end run
APPLESCRIPT
    if ! play_desktop_sound && is_true "$NOTIFY_DESKTOP_SYSTEM_CHIME"; then
      osascript -e 'beep 1' >/dev/null 2>&1 || true
    fi
    echo "{\"message\":\"desktop notification sent\"}"
    return 0
  fi

  if command -v notify-send >/dev/null 2>&1; then
    notify-send "Codex Task" "$msg"
    if ! play_desktop_sound && is_true "$NOTIFY_DESKTOP_SYSTEM_CHIME" && command -v osascript >/dev/null 2>&1; then
      osascript -e 'beep 1' >/dev/null 2>&1 || true
    fi
    echo "{\"message\":\"desktop notification sent\"}"
    return 0
  fi

  echo "No desktop notifier available" >&2
  return 1
}

send_telegram() {
  local details="$1"
  local env_file="$TELEGRAM_ENV_FILE_DEFAULT"
  [[ -n "$ENV_FILE" ]] && env_file="$ENV_FILE"

  if [[ ! -f "$env_file" ]]; then
    echo "missing telegram env file: $env_file" >&2
    return 1
  fi

  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a

  local token="${TELEGRAM_BOT_TOKEN:-}"
  local chat_id="${TELEGRAM_CHAT_ID:-}"

  if [[ -z "$token" || -z "$chat_id" ]]; then
    echo "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in $env_file" >&2
    return 1
  fi

  local msg
  if [[ -n "$BODY" ]]; then
    msg="$BODY"
  else
    local icon
    [[ "$STATUS" == "success" ]] && icon="✅" || icon="❌"
    msg="${icon} Claude"
    [[ -n "$TASK" ]] && msg="${msg} | ${TASK}"
    [[ -n "$details" ]] && msg="${msg} | ${details}"
  fi

  if [[ "$DRY_RUN" == true ]]; then
    python3 -c "import json,sys; print(json.dumps({'mode':'dry-run','message':sys.argv[1]}))" "$msg"
    return 0
  fi

  local api_url="https://api.telegram.org/bot${token}/sendMessage"
  local payload="{\"chat_id\":\"${chat_id}\",\"text\":\"${msg}\"}"

  if command -v curl >/dev/null 2>&1; then
    curl -sf -X POST "$api_url" \
      -H "Content-Type: application/json" \
      -d "$payload"
  else
    python3 - "$token" "$chat_id" "$msg" <<'PYEOF'
import json, sys, urllib.request
token, chat_id, text = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.dumps({"chat_id": chat_id, "text": text}).encode()
req = urllib.request.Request(
    f"https://api.telegram.org/bot{token}/sendMessage",
    data=data, headers={"Content-Type": "application/json"}
)
with urllib.request.urlopen(req, timeout=10) as r:
    print(r.read().decode())
PYEOF
  fi
}

main() {
  local details started
  started="$STARTED_AT"
  [[ -z "$started" ]] && started="${NOTIFY_STARTED_AT_EPOCH:-}"
  details="$(append_context "$DETAILS" "$started")"

  local dedupe_basis dedupe_hash
  dedupe_basis="channel=$CHANNEL|status=$STATUS|task=$TASK|details=$details|body=$BODY"
  dedupe_hash="$(hash_text "$dedupe_basis")"

  if [[ "$FORCE" != true ]] && is_duplicate "$dedupe_hash"; then
    log_line "skip" "$CHANNEL" "duplicate"
    echo '{"status":"skipped","reason":"duplicate"}'
    return 0
  fi

  if [[ "$URGENT" != true ]] && is_quiet_time "$NOTIFY_QUIET_HOURS"; then
    if [[ "$STATUS" == "failure" ]] && is_true "$NOTIFY_FAILURE_BYPASS_QUIET"; then
      :
    else
      log_line "skip" "$CHANNEL" "quiet_hours"
      echo '{"status":"skipped","reason":"quiet_hours"}'
      return 0
    fi
  fi

  local channels=()
  case "$CHANNEL" in
    auto) channels=(telegram sms whatsapp desktop) ;;
    sms) channels=(sms) ;;
    whatsapp) channels=(whatsapp) ;;
    desktop) channels=(desktop) ;;
    telegram) channels=(telegram) ;;
  esac

  local ch out
  for ch in "${channels[@]}"; do
    case "$ch" in
      sms)
        if out="$(send_sms "$details" 2>&1)"; then
          :
        else
          log_line "error" "$ch" "$out"
          continue
        fi
        ;;
      whatsapp)
        if out="$(send_whatsapp "$details" 2>&1)"; then
          :
        else
          log_line "error" "$ch" "$out"
          continue
        fi
        ;;
      desktop)
        if out="$(send_desktop "$details" 2>&1)"; then
          :
        else
          log_line "error" "$ch" "$out"
          continue
        fi
        ;;
      telegram)
        if out="$(send_telegram "$details" 2>&1)"; then
          :
        else
          log_line "error" "$ch" "$out"
          continue
        fi
        ;;
    esac

    if [[ -n "$out" ]]; then
      [[ "$DRY_RUN" == true ]] || record_hash "$dedupe_hash"
      log_line "sent" "$ch" "ok"
      json_wrap "$ch" "$out"
      return 0
    fi

    log_line "error" "$ch" "empty output"
  done

  echo "ERROR: all notification channels failed" >&2
  echo "See log: $LOG_FILE" >&2
  return 1
}

main
