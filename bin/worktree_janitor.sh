#!/usr/bin/env bash
# worktree_janitor.sh — prune stale agent worktrees under ~/.claude/state/worktrees.
#
# Removes worktree entries older than --older-than-days (default 14) that are
# CLEAN (no uncommitted/untracked changes). Dirty worktrees are skipped and
# reported. Committed-but-unpushed work is safe to remove: `git worktree remove`
# leaves the branch ref in the parent repo, so the commits stay reachable and the
# worktree can be recreated with `git worktree add <path> <branch>`.
#
# Flags:
#   --older-than-days N   age threshold by directory mtime (default 14)
#   --dry-run             report actions, change nothing
#
# Removal logic mirrors ~/.claude/hooks/worktree_remove.sh. Failures are logged,
# never fatal. Logs to ~/.claude/state/hooks/worktree.log.

set -u

ROOT="${WORKTREE_ROOT:-${HOME}/.claude/state/worktrees}"
DAYS=14
DRY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --older-than-days) DAYS="$2"; shift 2 ;;
    --older-than-days=*) DAYS="${1#*=}"; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

LOG_DIR="${HOME}/.claude/state/hooks"
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG_FILE="$LOG_DIR/worktree.log"
log() { printf '[%s] worktree_janitor %s\n' "$(date +%Y-%m-%dT%H:%M:%S%z)" "$*" >> "$LOG_FILE"; }

[ -d "$ROOT" ] || { echo "no worktree root: $ROOT"; exit 0; }

removed=0; skipped_dirty=0; failed=0; removed_kb=0

while IFS= read -r wt; do
  [ -n "$wt" ] || continue
  # dirty check (works for linked + plain dirs; plain fallback dirs are git-init'd)
  if git -C "$wt" rev-parse --git-dir >/dev/null 2>&1; then
    if [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]; then
      echo "SKIP dirty: $wt"
      log "SKIP dirty path=$wt"
      skipped_dirty=$((skipped_dirty+1))
      continue
    fi
  fi
  sz=$(du -sk "$wt" 2>/dev/null | cut -f1); sz=${sz:-0}
  if [ "$DRY" -eq 1 ]; then
    echo "WOULD REMOVE (${sz}KB): $wt"
    removed=$((removed+1)); removed_kb=$((removed_kb+sz))
    continue
  fi
  ok=0
  if [ -f "$wt/.git" ]; then
    gitdir=$(sed -n 's/^gitdir:[[:space:]]*\(.*\)$/\1/p' "$wt/.git")
    parent_repo="${gitdir%/.git/worktrees/*}"
    if [ -n "$parent_repo" ] && [ -d "$parent_repo" ]; then
      if git -C "$parent_repo" worktree remove --force "$wt" >>"$LOG_FILE" 2>&1; then
        git -C "$parent_repo" worktree prune >>"$LOG_FILE" 2>&1 || true
        ok=1; log "OK git-worktree-remove path=$wt"
      fi
    fi
  fi
  if [ "$ok" -eq 0 ]; then
    if rm -rf "$wt" 2>>"$LOG_FILE"; then ok=1; log "OK rm-rf path=$wt"; fi
  fi
  if [ "$ok" -eq 1 ]; then
    removed=$((removed+1)); removed_kb=$((removed_kb+sz)); echo "removed (${sz}KB): $wt"
  else
    failed=$((failed+1)); log "FAIL path=$wt"; echo "FAIL: $wt"
  fi
done < <(find "$ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +"$DAYS" 2>/dev/null)

mb=$((removed_kb/1024))
mode="purge"; [ "$DRY" -eq 1 ] && mode="dry-run"
echo "---"
echo "$mode: candidates=$removed dirty-skipped=$skipped_dirty failed=$failed reclaim=${mb}MB threshold=${DAYS}d"
log "$mode candidates=$removed dirty_skipped=$skipped_dirty failed=$failed reclaim_mb=${mb} threshold=${DAYS}d"
