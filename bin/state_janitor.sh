#!/usr/bin/env bash
# state_janitor.sh — single daily retention sweep for ~/.claude/state.
#
# Replaces the per-directory worktree-only janitor. Three jobs:
#   1. worktrees  — delegates to worktree_janitor.sh (clean + older-than N days)
#   2. JSONL logs — append-only logs over a size threshold are gzip-archived
#                   to state/log-archive/, then truncated to the last K lines
#                   (full history preserved compressed).
#   3. tracks     — TERMINAL autonomy tracks older than N days are tar.gz'd to
#                   state/track-archive/ then removed. Non-terminal tracks are
#                   never touched (they may be resumable).
#
# Flags:
#   --dry-run                 report actions, change nothing
#   --log-threshold-kb N      rotate logs larger than N KB   (default 1024)
#   --keep-lines N            lines kept live after rotation  (default 5000)
#   --worktree-days N         worktree age threshold          (default 14)
#   --track-days N            terminal-track age threshold     (default 30)
#
# Logs to ~/.claude/state/hooks/state-janitor.log. Failures are logged, never fatal.

set -u

STATE="${CLAUDE_STATE_DIR:-${HOME}/.claude/state}"
BIN="${HOME}/.claude/bin"
AUTONOMY_DIR="$STATE/autonomy"
ARCHIVE_DIR="$STATE/log-archive"
TRACK_ARCHIVE="$STATE/track-archive"
LOG_DIR="$STATE/hooks"
LOG_FILE="$LOG_DIR/state-janitor.log"

DRY=0
LOG_THRESHOLD_KB=1024
KEEP_LINES=5000
WT_DAYS=14
TRACK_DAYS=30

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --log-threshold-kb) LOG_THRESHOLD_KB="$2"; shift 2 ;;
    --keep-lines) KEEP_LINES="$2"; shift 2 ;;
    --worktree-days) WT_DAYS="$2"; shift 2 ;;
    --track-days) TRACK_DAYS="$2"; shift 2 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$LOG_DIR" "$ARCHIVE_DIR" "$TRACK_ARCHIVE" 2>/dev/null || true
log() { printf '[%s] state_janitor %s\n' "$(date +%Y-%m-%dT%H:%M:%S%z)" "$*" >> "$LOG_FILE"; }
DRYFLAG=""; [ "$DRY" -eq 1 ] && DRYFLAG="--dry-run"
threshold_bytes=$((LOG_THRESHOLD_KB * 1024))

echo "=== [1/3] worktrees ==="
if [ -x "$BIN/worktree_janitor.sh" ]; then
  "$BIN/worktree_janitor.sh" $DRYFLAG --older-than-days "$WT_DAYS" | tail -2
else
  echo "  (worktree_janitor.sh missing — skipped)"
fi

echo "=== [2/3] JSONL log rotation (>${LOG_THRESHOLD_KB}KB -> archive + keep last ${KEEP_LINES}) ==="
rotated=0
for f in "$STATE"/*.jsonl; do
  [ -f "$f" ] || continue
  bytes=$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null || echo 0)
  [ "$bytes" -ge "$threshold_bytes" ] || continue
  lines=$(wc -l < "$f" 2>/dev/null | tr -d ' '); lines=${lines:-0}
  [ "$lines" -gt "$KEEP_LINES" ] || continue   # already at/under keep — don't re-archive same lines
  base=$(basename "$f")
  if [ "$DRY" -eq 1 ]; then
    echo "  WOULD ROTATE ($((bytes/1024))KB): $base"; rotated=$((rotated+1)); continue
  fi
  arch="$ARCHIVE_DIR/${base}.$(date +%Y%m%dT%H%M%S).gz"
  if gzip -c "$f" > "$arch" 2>/dev/null; then
    if tail -n "$KEEP_LINES" "$f" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"; then
      echo "  rotated ($((bytes/1024))KB -> last ${KEEP_LINES}): $base"
      log "rotated $base bytes=$bytes archive=$arch"
      rotated=$((rotated+1))
    else
      rm -f "$f.tmp" 2>/dev/null; echo "  FAIL truncate: $base"; log "FAIL truncate $base"
    fi
  else
    echo "  FAIL archive: $base"; log "FAIL archive $base"
  fi
done
[ "$rotated" -eq 0 ] && echo "  (no logs over threshold)"

echo "=== [3/3] terminal autonomy tracks older than ${TRACK_DAYS}d ==="
archived_tracks=0
if [ -d "$AUTONOMY_DIR" ]; then
  while IFS= read -r td; do
    [ -n "$td" ] || continue
    cj="$td/objective.closure.json"
    [ -f "$cj" ] || continue
    grep -q '"terminal": true' "$cj" 2>/dev/null || continue   # skip non-terminal
    name=$(basename "$td")
    if [ "$DRY" -eq 1 ]; then
      echo "  WOULD ARCHIVE track: $name"; archived_tracks=$((archived_tracks+1)); continue
    fi
    if tar -czf "$TRACK_ARCHIVE/${name}.tar.gz" -C "$AUTONOMY_DIR" "$name" 2>/dev/null && rm -rf "$td"; then
      echo "  archived+removed: $name"; log "track_archived $name"; archived_tracks=$((archived_tracks+1))
    else
      echo "  FAIL archive track: $name"; log "FAIL track_archive $name"
    fi
  done < <(find "$AUTONOMY_DIR" -mindepth 1 -maxdepth 1 -type d -mtime +"$TRACK_DAYS" 2>/dev/null)
fi
[ "$archived_tracks" -eq 0 ] && echo "  (no terminal tracks over age)"

mode="sweep"; [ "$DRY" -eq 1 ] && mode="dry-run"
echo "--- state_janitor $mode: logs_rotated=$rotated tracks_archived=$archived_tracks ---"
log "$mode logs_rotated=$rotated tracks_archived=$archived_tracks"
