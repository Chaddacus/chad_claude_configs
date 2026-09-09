"""Config checkpoint/restore system for autoconfig.

Manages snapshots of the Claude configuration surface so that
autoconfig experiments can be safely applied and rolled back.

Snapshot slots:
  baseline   — current known-good config
  checkpoint — pre-experiment snapshot (for rollback)
  best       — best-ever config snapshot

Crash recovery: if a checkpoint exists without a .completed marker,
the daemon should restore it on startup to undo a partially-applied
experiment.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAUDE_HOME: Path = Path.home() / ".claude"

# Config files relative to CLAUDE_HOME (non-agent files).
CONFIG_FILES: list[str] = [
    "state/route_manifest.json",
    "settings.json",
]

# Agent definitions directory, relative to CLAUDE_HOME.
AGENT_DIR: str = "agents"

# Well-known agent filenames inside AGENT_DIR.
_AGENT_FILES: list[str] = [
    "worker.md",
    "planner.md",
    "reviewer.md",
    "explorer.md",
    "validator.md",
]

SNAPSHOT_BASE: Path = CLAUDE_HOME / "state" / "autoconfig"

_VALID_TARGETS = frozenset({"baseline", "checkpoint", "best"})

_META_FILENAME = ".snapshot_meta.json"
_COMPLETED_MARKER = ".completed"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _all_relative_paths() -> list[str]:
    """Return a sorted list of all config-surface paths relative to CLAUDE_HOME."""
    paths = list(CONFIG_FILES)
    for agent_file in _AGENT_FILES:
        paths.append(f"{AGENT_DIR}/{agent_file}")
    paths.sort()
    return paths


def _validate_target(target: str) -> Path:
    """Validate that *target* is a known slot and return its directory."""
    if target not in _VALID_TARGETS:
        raise ValueError(
            f"Invalid snapshot target {target!r}; "
            f"must be one of {sorted(_VALID_TARGETS)}"
        )
    return SNAPSHOT_BASE / target


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_config_hash() -> str:
    """SHA-256 of concatenated config file contents (sorted by path).

    Files that do not exist on disk are silently skipped so that
    a partial config surface still produces a stable hash.
    """
    h = hashlib.sha256()
    for rel in _all_relative_paths():
        src = CLAUDE_HOME / rel
        if src.is_file():
            h.update(rel.encode("utf-8"))
            h.update(src.read_bytes())
    return h.hexdigest()


def save_snapshot(target: str, score: Optional[float] = None) -> Path:
    """Copy live config files into the *target* snapshot slot.

    Parameters
    ----------
    target : str
        One of ``"baseline"``, ``"checkpoint"``, or ``"best"``.
    score : float | None
        Optional quality score to record in the snapshot metadata.

    Returns
    -------
    Path
        The snapshot directory that was written.
    """
    snap_dir = _validate_target(target)

    # Wipe previous snapshot contents so stale files don't linger.
    if snap_dir.exists():
        shutil.rmtree(snap_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)

    for rel in _all_relative_paths():
        src = CLAUDE_HOME / rel
        if not src.is_file():
            continue
        dst = snap_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # Write metadata.
    meta = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config_hash": compute_config_hash(),
    }
    if score is not None:
        meta["score"] = score

    (snap_dir / _META_FILENAME).write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )

    return snap_dir


def restore_snapshot(source: str) -> bool:
    """Restore config files from *source* snapshot back to their live locations.

    Parameters
    ----------
    source : str
        One of ``"baseline"``, ``"checkpoint"``, or ``"best"``.

    Returns
    -------
    bool
        ``True`` if the snapshot existed and was restored; ``False`` otherwise.
    """
    snap_dir = _validate_target(source)
    if not snap_dir.is_dir():
        return False

    restored_any = False
    for rel in _all_relative_paths():
        src = snap_dir / rel
        if not src.is_file():
            continue
        dst = CLAUDE_HOME / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored_any = True

    return restored_any


def snapshot_exists(target: str) -> bool:
    """Return ``True`` if a snapshot has been saved for *target*."""
    snap_dir = _validate_target(target)
    return (snap_dir / _META_FILENAME).is_file()


def get_snapshot_meta(target: str) -> Optional[dict]:
    """Read ``.snapshot_meta.json`` from a snapshot slot.

    Returns ``None`` if the snapshot or its metadata file does not exist.
    """
    snap_dir = _validate_target(target)
    meta_file = snap_dir / _META_FILENAME
    if not meta_file.is_file():
        return None
    return json.loads(meta_file.read_text(encoding="utf-8"))


def has_dirty_checkpoint() -> bool:
    """Return ``True`` if a checkpoint exists but has no completion marker.

    A dirty checkpoint means an experiment was started but never
    resolved (keep or discard), likely due to a crash.
    """
    checkpoint_dir = SNAPSHOT_BASE / "checkpoint"
    if not (checkpoint_dir / _META_FILENAME).is_file():
        return False
    return not (checkpoint_dir / _COMPLETED_MARKER).is_file()


def mark_checkpoint_clean() -> None:
    """Write a ``.completed`` marker to the checkpoint directory.

    This signals that the experiment that created the checkpoint has
    been resolved (either kept or discarded) and no crash recovery is
    needed.
    """
    checkpoint_dir = SNAPSHOT_BASE / "checkpoint"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    (checkpoint_dir / _COMPLETED_MARKER).write_text(
        datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8"
    )


def clear_checkpoint() -> None:
    """Remove the checkpoint directory and all its contents."""
    checkpoint_dir = SNAPSHOT_BASE / "checkpoint"
    if checkpoint_dir.exists():
        shutil.rmtree(checkpoint_dir)
