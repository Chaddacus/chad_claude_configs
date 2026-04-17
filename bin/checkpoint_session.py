#!/usr/bin/env python3
"""Stop hook — save session checkpoint for continuity.

Writes a checkpoint file when meaningful edits occurred during the session.
Complements completion_gate.py (which validates); this persists state.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

session_id = os.environ.get("CLAUDE_SESSION_ID", "unknown")
claude_home = Path(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")))

# Check if there's a verification ledger (edit_verify_async creates these)
ledger_path = Path(f"/tmp/claude-verify-{session_id}.json")
if not ledger_path.exists():
    sys.exit(0)

try:
    ledger = json.loads(ledger_path.read_text())
except Exception:
    sys.exit(0)

edited_files = ledger.get("edited_files", [])
if not edited_files:
    sys.exit(0)

# Write checkpoint
checkpoint_dir = claude_home / "checkpoints"
checkpoint_dir.mkdir(exist_ok=True)

checkpoint = {
    "session_id": session_id,
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "edited_files": edited_files[:20],
    "verified": ledger.get("last_verified_at") is not None,
    "cwd": os.environ.get("PWD", os.getcwd()),
}

checkpoint_file = checkpoint_dir / f"{session_id}.json"
checkpoint_file.write_text(json.dumps(checkpoint, indent=2) + "\n")

# Prune old checkpoints (keep last 20)
checkpoints = sorted(checkpoint_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
for old in checkpoints[:-20]:
    old.unlink(missing_ok=True)

sys.exit(0)
