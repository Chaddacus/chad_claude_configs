#!/usr/bin/env bash
set -euo pipefail

CHANNEL="sms"
ENV_FILE=""

usage() {
  cat <<'EOF'
Usage:
  check_notifications_env.sh [options]

Options:
  --channel <sms|whatsapp>   Which channel to validate (default: sms)
  --env-file <path>          Override env file path
  -h, --help                 Show this help

Default env files:
  sms:      ~/.config/codex/secrets/twilio.env
  whatsapp: ~/.config/codex/secrets/whatsapp.env
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel)
      CHANNEL="${2:-}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
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

if [[ "$CHANNEL" != "sms" && "$CHANNEL" != "whatsapp" ]]; then
  echo "ERROR: --channel must be sms or whatsapp" >&2
  exit 1
fi

if [[ -z "$ENV_FILE" ]]; then
  if [[ "$CHANNEL" == "sms" ]]; then
    ENV_FILE="${HOME}/.config/codex/secrets/twilio.env"
  else
    ENV_FILE="${HOME}/.config/codex/secrets/whatsapp.env"
  fi
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: env file not found: $ENV_FILE" >&2
  exit 1
fi

if [[ ! -r "$ENV_FILE" ]]; then
  echo "ERROR: env file is not readable: $ENV_FILE" >&2
  exit 1
fi

perm="$(stat -f '%Mp%Lp' "$ENV_FILE" 2>/dev/null || true)"
if [[ -n "$perm" && "$perm" != "600" && "$perm" != "400" && "$perm" != "0600" && "$perm" != "0400" ]]; then
  echo "WARN: env file permissions are $perm (recommended: 600 or 400)" >&2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ "$CHANNEL" == "sms" ]]; then
  required=(
    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_FROM_NUMBER
    TWILIO_TO_NUMBER
  )

  missing=()
  for key in "${required[@]}"; do
    if [[ -z "${!key:-}" ]]; then
      missing+=("$key")
    fi
  done

  if (( ${#missing[@]} > 0 )); then
    echo "ERROR: missing required sms variables in $ENV_FILE:" >&2
    for key in "${missing[@]}"; do
      echo "  - $key" >&2
    done

    if [[ -n "${Twilio_Account_SID:-}" ]]; then
      echo "HINT: found Twilio_Account_SID; expected TWILIO_ACCOUNT_SID" >&2
    fi
    exit 1
  fi

  if [[ ! "${TWILIO_ACCOUNT_SID}" =~ ^AC[a-zA-Z0-9]{32}$ ]]; then
    echo "ERROR: TWILIO_ACCOUNT_SID format is invalid (expected AC + 32 alphanumeric chars)." >&2
    exit 1
  fi

  if [[ ! "${TWILIO_FROM_NUMBER}" =~ ^\+[1-9][0-9]{6,14}$ ]]; then
    echo "ERROR: TWILIO_FROM_NUMBER must be E.164 format, e.g. +15551234567" >&2
    exit 1
  fi

  if [[ ! "${TWILIO_TO_NUMBER}" =~ ^\+[1-9][0-9]{6,14}$ ]]; then
    echo "ERROR: TWILIO_TO_NUMBER must be E.164 format, e.g. +15551234567" >&2
    exit 1
  fi

  if (( ${#TWILIO_AUTH_TOKEN} < 8 )); then
    echo "ERROR: TWILIO_AUTH_TOKEN looks too short." >&2
    exit 1
  fi

  echo "OK: sms notification environment is valid ($ENV_FILE)"
  exit 0
fi

required=(
  META_WHATSAPP_ACCESS_TOKEN
  META_WHATSAPP_PHONE_NUMBER_ID
  WHATSAPP_TO_NUMBER
)

missing=()
for key in "${required[@]}"; do
  if [[ -z "${!key:-}" ]]; then
    missing+=("$key")
  fi
done

if (( ${#missing[@]} > 0 )); then
  echo "ERROR: missing required whatsapp variables in $ENV_FILE:" >&2
  for key in "${missing[@]}"; do
    echo "  - $key" >&2
  done
  exit 1
fi

if [[ ! "${META_WHATSAPP_PHONE_NUMBER_ID}" =~ ^[0-9]{6,20}$ ]]; then
  echo "ERROR: META_WHATSAPP_PHONE_NUMBER_ID must be 6-20 digits." >&2
  exit 1
fi

api_version="${META_WHATSAPP_API_VERSION:-v21.0}"
if [[ ! "$api_version" =~ ^v[0-9]+\.[0-9]+$ ]]; then
  echo "ERROR: META_WHATSAPP_API_VERSION must look like v21.0" >&2
  exit 1
fi

if (( ${#META_WHATSAPP_ACCESS_TOKEN} < 16 )); then
  echo "ERROR: META_WHATSAPP_ACCESS_TOKEN looks too short." >&2
  exit 1
fi

echo "OK: whatsapp notification environment is valid ($ENV_FILE)"
