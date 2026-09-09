#!/usr/bin/env python3
"""ConfigChange hook — diff config sources vs last known hash, journal drift.

For each fire, hash the relevant settings file (matched by `source` /
matcher value) and compare against the last-seen hash. Emit a JSONL drift
record. The cached hash is updated on every fire so subsequent runs
diff against the most recent state, not the original.

Logs to: ~/.claude/state/config-drift.jsonl
Hashes:  ~/.claude/state/config-hashes/<source>.hash
Stdout:  (empty — exit 0; this implementation never blocks the change)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".claude" / "state"
LOG = STATE_DIR / "config-drift.jsonl"
HASH_DIR = STATE_DIR / "config-hashes"


def _resolve_target(source: str) -> Path | None:
    """Map matcher value -> file path. project_settings/local_settings depend on cwd."""
    home = Path.home()
    cwd = Path(os.getcwd())
    mapping = {
        "user_settings": home / ".claude" / "settings.json",
        "project_settings": cwd / ".claude" / "settings.json",
        "local_settings": cwd / ".claude" / "settings.local.json",
        "policy_settings": home / ".claude" / "CLAUDE.md",
        # `skills` matcher targets the skills directory tree — we hash a manifest of names.
        "skills": home / ".claude" / "skills",
    }
    return mapping.get(source)


def _hash_target(target: Path) -> tuple[str, int]:
    if not target.exists():
        return ("missing", 0)
    if target.is_file():
        try:
            data = target.read_bytes()
            return (hashlib.sha256(data).hexdigest()[:16], len(data))
        except Exception:
            return ("unreadable", 0)
    if target.is_dir():
        # Hash a sorted manifest of immediate child names so adds/removes show up.
        try:
            names = sorted(p.name for p in target.iterdir())
            payload = "\n".join(names).encode()
            return (hashlib.sha256(payload).hexdigest()[:16], len(payload))
        except Exception:
            return ("unreadable", 0)
    return ("unknown", 0)


def main() -> int:
    try:
        evt = json.load(sys.stdin)
    except Exception:
        return 0

    source = evt.get("source") or evt.get("matcher", "") or ""
    target = _resolve_target(source)

    new_hash, size = ("", 0)
    if target is not None:
        new_hash, size = _hash_target(target)

    HASH_DIR.mkdir(parents=True, exist_ok=True)
    cache = HASH_DIR / f"{source or 'unknown'}.hash"
    prev_hash = ""
    if cache.exists():
        try:
            prev_hash = cache.read_text(encoding="utf-8").strip()
        except Exception:
            prev_hash = ""

    rec = {
        "ts": time.time(),
        "source": source,
        "target": str(target) if target else "",
        "prev_hash": prev_hash,
        "new_hash": new_hash,
        "size_bytes": size,
        "first_observation": prev_hash == "",
        "changed": (prev_hash != new_hash) and (prev_hash != ""),
        "raw_event": evt,
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass

    if new_hash and new_hash not in {"unreadable", "missing", "unknown"}:
        try:
            cache.write_text(new_hash, encoding="utf-8")
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
