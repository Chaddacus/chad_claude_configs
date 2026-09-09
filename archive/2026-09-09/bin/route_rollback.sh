#!/usr/bin/env bash
set -euo pipefail

APP_HOME="${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}"
BACKUP_DIR=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup-dir)
      BACKUP_DIR="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --help|-h)
      cat <<'USAGE'
Usage: route_rollback.sh [--backup-dir PATH] [--dry-run]

Restores router-related config TOMLs from a backup directory and removes
"Routing Contract v1 (Model + Effort)" section from CLAUDE.md/CLAUDE.md.

If --backup-dir is omitted, the latest timestamped backup under
$APP_HOME/state/backups is used.
USAGE
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$BACKUP_DIR" ]]; then
  BACKUP_DIR="$(ls -dt "$APP_HOME"/state/backups/* "$APP_HOME"/backups/codex-sync-* 2>/dev/null | head -n 1 || true)"
fi

if [[ -z "$BACKUP_DIR" || ! -d "$BACKUP_DIR" ]]; then
  echo "Backup directory not found: $BACKUP_DIR" >&2
  exit 1
fi

copy_restore() {
  local src="$1"
  local dst="$2"
  if [[ ! -f "$src" ]]; then
    echo "skip missing backup file: $src"
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  if [[ "$DRY_RUN" == true ]]; then
    echo "DRY-RUN cp '$src' '$dst'"
  else
    cp "$src" "$dst"
    echo "restored: $dst"
  fi
}

copy_restore "$BACKUP_DIR/config.toml.bak" "$APP_HOME/config.toml"
copy_restore "$BACKUP_DIR/worker.toml.bak" "$APP_HOME/agents/worker.toml"
copy_restore "$BACKUP_DIR/explorer.toml.bak" "$APP_HOME/agents/explorer.toml"
copy_restore "$BACKUP_DIR/planner.toml.bak" "$APP_HOME/agents/planner.toml"
copy_restore "$BACKUP_DIR/reviewer.toml.bak" "$APP_HOME/agents/reviewer.toml"

AGENTS_FILE="$APP_HOME/CLAUDE.md"
if [[ ! -f "$AGENTS_FILE" ]]; then
  AGENTS_FILE="$APP_HOME/CLAUDE.md"
fi

if [[ "$DRY_RUN" == true ]]; then
  echo "DRY-RUN remove section: '## Routing Contract v1 (Model + Effort)' from $AGENTS_FILE"
  exit 0
fi

if [[ ! -f "$AGENTS_FILE" ]]; then
  echo "No CLAUDE.md/CLAUDE.md found at $APP_HOME; skipping section removal"
  echo "rollback complete from backup: $BACKUP_DIR"
  exit 0
fi

python3 - "$AGENTS_FILE" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

start_pattern = re.compile(r"^## Routing Contract v1 \(Model \+ Effort\)\n", re.MULTILINE)
start_match = start_pattern.search(text)
if not start_match:
    print("routing contract section not found; no AGENTS edits applied")
    sys.exit(0)

# Remove from routing contract header through the next horizontal rule line.
post_start = text[start_match.start():]
end_match = re.search(r"^---\n", post_start, re.MULTILINE)
if end_match is None:
    print("unable to find section terminator '---' after routing contract header", file=sys.stderr)
    sys.exit(1)

remove_end = start_match.start() + end_match.end()
new_text = text[:start_match.start()] + text[remove_end:]

# Normalize extra blank lines left by section removal.
new_text = re.sub(r"\n{3,}", "\n\n", new_text)
path.write_text(new_text, encoding="utf-8")
print(f"removed routing contract section from {path.name}")
PY

echo "rollback complete from backup: $BACKUP_DIR"
