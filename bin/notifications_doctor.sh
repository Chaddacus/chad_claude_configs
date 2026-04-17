#!/usr/bin/env bash
set -euo pipefail

CLAUDE_HOME_DIR="${CLAUDE_HOME:-${CODEX_HOME:-${HOME}/.claude}}"
CHECK_SCRIPT="$CLAUDE_HOME_DIR/bin/check_notifications_env.sh"
NOTIFY_SCRIPT="$CLAUDE_HOME_DIR/bin/notify_done.sh"
SMS_ENV_FILE="${HOME}/.config/codex/secrets/twilio.env"
WHATSAPP_ENV_FILE="${HOME}/.config/codex/secrets/whatsapp.env"
RUN_LIVE=false

usage() {
  cat <<'EOF'
Usage:
  notifications_doctor.sh [options]

Options:
  --sms-env-file <path>       Override SMS env file
  --whatsapp-env-file <path>  Override WhatsApp env file
  --live                      Send one live test on available channels
  -h, --help                  Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sms-env-file)
      SMS_ENV_FILE="${2:-}"
      shift 2
      ;;
    --whatsapp-env-file)
      WHATSAPP_ENV_FILE="${2:-}"
      shift 2
      ;;
    --live)
      RUN_LIVE=true
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

[[ -x "$CHECK_SCRIPT" ]] || { echo "ERROR: missing check script: $CHECK_SCRIPT" >&2; exit 1; }
[[ -x "$NOTIFY_SCRIPT" ]] || { echo "ERROR: missing notify script: $NOTIFY_SCRIPT" >&2; exit 1; }

sms_ok=false
whatsapp_ok=false

printf '== Notifications Doctor ==\n'
printf 'claude_home: %s\n' "$CLAUDE_HOME_DIR"
printf 'notify_script: %s\n' "$NOTIFY_SCRIPT"
printf 'check_script: %s\n' "$CHECK_SCRIPT"

printf '\n[1/4] Validate SMS env...\n'
if "$CHECK_SCRIPT" --channel sms --env-file "$SMS_ENV_FILE"; then
  sms_ok=true
else
  printf 'SMS validation failed\n' >&2
fi

printf '\n[2/4] Validate WhatsApp env...\n'
if "$CHECK_SCRIPT" --channel whatsapp --env-file "$WHATSAPP_ENV_FILE"; then
  whatsapp_ok=true
else
  printf 'WhatsApp validation failed (optional unless you use WhatsApp fallback).\n' >&2
fi

printf '\n[3/4] Dry-run send checks...\n'
if [[ "$sms_ok" == true ]]; then
  "$NOTIFY_SCRIPT" --channel sms --status success --task 'doctor sms dry-run' --details 'env ok' --env-file "$SMS_ENV_FILE" --force --dry-run
else
  printf 'Skipping SMS dry-run (env invalid).\n'
fi

if [[ "$whatsapp_ok" == true ]]; then
  "$NOTIFY_SCRIPT" --channel whatsapp --status success --task 'doctor whatsapp dry-run' --details 'env ok' --env-file "$WHATSAPP_ENV_FILE" --force --dry-run
else
  printf 'Skipping WhatsApp dry-run (env invalid).\n'
fi

printf '\n[4/4] Optional live send...\n'
if [[ "$RUN_LIVE" == true ]]; then
  if [[ "$sms_ok" == true ]]; then
    "$NOTIFY_SCRIPT" --channel sms --status success --task 'doctor sms live' --details 'live test' --env-file "$SMS_ENV_FILE" --force
  else
    printf 'Skipping SMS live test (env invalid).\n'
  fi

  if [[ "$whatsapp_ok" == true ]]; then
    "$NOTIFY_SCRIPT" --channel whatsapp --status success --task 'doctor whatsapp live' --details 'live test' --env-file "$WHATSAPP_ENV_FILE" --force
  else
    printf 'Skipping WhatsApp live test (env invalid).\n'
  fi
else
  printf 'Live send skipped (pass --live to enable).\n'
fi

printf '\nSummary:\n'
printf '  SMS env: %s\n' "$sms_ok"
printf '  WhatsApp env: %s\n' "$whatsapp_ok"
printf '  live test: %s\n' "$RUN_LIVE"
