#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ -z "${STITCH_API_KEY:-}" ]]; then
  echo "STITCH_API_KEY is not configured in $ENV_FILE" >&2
  exit 1
fi

exec node "$SCRIPT_DIR/dist/stitch-mcp.cjs"
