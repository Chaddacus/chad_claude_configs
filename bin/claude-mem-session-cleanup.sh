#!/bin/bash
# Claude-mem session cleanup safety net
# Marks stale "active" sessions as "completed" and performs maintenance

DB="$HOME/.claude-mem/claude-mem.db"
[ ! -f "$DB" ] && exit 0

TWO_HOURS_AGO_MS=$(( ($(date +%s) - 7200) * 1000 ))
NOW_MS=$(( $(date +%s) * 1000 ))
NOW_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Mark stale active sessions as completed (not failed)
UPDATED=$(sqlite3 "$DB" "UPDATE sdk_sessions SET status = 'completed', completed_at = '$NOW_ISO', completed_at_epoch = $NOW_MS WHERE status = 'active' AND started_at_epoch < $TWO_HOURS_AGO_MS; SELECT changes();")
[ "$UPDATED" -gt 0 ] 2>/dev/null && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Marked $UPDATED stale sessions as completed"

# WAL checkpoint (passive, non-blocking)
sqlite3 "$DB" "PRAGMA wal_checkpoint(PASSIVE);" > /dev/null 2>&1

# Log rotation: delete logs older than 7 days
find "$HOME/.claude-mem/logs" -name "*.log" -mtime +7 -delete 2>/dev/null

exit 0
