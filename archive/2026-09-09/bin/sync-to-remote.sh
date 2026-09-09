#!/usr/bin/env bash
# sync-to-remote.sh — Push the Claude harness to a remote dev box.
# Usage: bash ~/.claude/bin/sync-to-remote.sh [host]
# Default host: noob-root (see ~/.ssh/config)

set -e

HOST="${1:-noob-root}"
REMOTE_HOME="/root"
REMOTE_CLAUDE="${REMOTE_HOME}/.claude"
LOCAL_CLAUDE="${HOME}/.claude"
SETTINGS_OVERRIDE="${LOCAL_CLAUDE}/remote-configs/noob-root-settings.json"

echo "==> Syncing Claude harness to ${HOST}:${REMOTE_CLAUDE}"
echo "    (excludes: codex plugin, secrets, cache, state, large dirs)"
echo ""

rsync -av --progress \
  --exclude='auth.json' \
  --exclude='secrets/' \
  --exclude='plugins/' \
  --exclude='state/' \
  --exclude='state_5.sqlite*' \
  --exclude='sessions/' \
  --exclude='history.jsonl' \
  --exclude='file-history/' \
  --exclude='projects/' \
  --exclude='debug/' \
  --exclude='telemetry/' \
  --exclude='log/' \
  --exclude='logs_*.sqlite*' \
  --exclude='cache/' \
  --exclude='shell-snapshots/' \
  --exclude='shell_snapshots/' \
  --exclude='paste-cache/' \
  --exclude='mcp-needs-auth-cache.json' \
  --exclude='models_cache.json' \
  --exclude='stats-cache.json' \
  --exclude='remote-settings.json' \
  --exclude='todos/' \
  --exclude='plans/' \
  --exclude='backups/' \
  --exclude='memory/' \
  --exclude='teams/' \
  --exclude='eval/' \
  --exclude='ide/' \
  --exclude='chrome/' \
  --exclude='statsig/' \
  --exclude='usage-data/' \
  --exclude='skills/codex-security/' \
  --exclude='skills/codex-branch/' \
  --exclude='skills/codex-delegate/' \
  --exclude='.pytest_cache/' \
  --exclude='.tmp/' \
  --exclude='tmp/' \
  --exclude='*.pyc' \
  --exclude='__pycache__/' \
  "${LOCAL_CLAUDE}/" "${HOST}:${REMOTE_CLAUDE}/"

echo ""
echo "==> Copying remote-specific settings.json"
scp "${SETTINGS_OVERRIDE}" "${HOST}:${REMOTE_CLAUDE}/settings.json"

echo ""
echo "==> Ensuring required directories exist on remote"
ssh "${HOST}" "mkdir -p ${REMOTE_CLAUDE}/state/locks ${REMOTE_CLAUDE}/sessions ${REMOTE_CLAUDE}/memory"

echo ""
echo "==> Verifying settings.json is valid JSON on remote"
ssh "${HOST}" "python3 -m json.tool ${REMOTE_CLAUDE}/settings.json > /dev/null && echo '    settings.json: valid'"

echo ""
echo "==> Done. Harness is on ${HOST}."
echo ""
echo "Next steps on ${HOST} (if first time):"
echo "  1. Install Claude Code CLI:"
echo "     curl -fsSL https://claude.ai/install.sh | sh"
echo "  2. Authenticate:"
echo "     claude login"
echo "  3. Verify hooks load:"
echo "     claude --print 'echo hello' 2>&1 | head -5"
