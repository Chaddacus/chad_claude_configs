#!/usr/bin/env python3
"""Apply auto-approved proposals to the orchestrator system.

Reads ~/.claude/evolve/proposals.jsonl, applies each unapplied proposal with
auto_apply=true, and logs the diff to ~/.claude/evolve/applied.jsonl.

Safety guardrails:
- `TARGET_ALLOWLIST`: only files whose path matches a pattern in this list can
  be modified. Everything else requires manual review.
- `DIFF_SIZE_CAP`: proposals adding more than 500 characters are rejected as
  auto-apply even if flagged (user must review).
- `NEVER_MODIFY`: exact paths that are never touched by auto-apply (e.g.,
  settings.json, auto_runtime.py, dispatcher source).

Usage:
    evolve_apply.py [--dry-run] [--force]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
PROPOSALS = HOME / ".claude" / "evolve" / "proposals.jsonl"
APPLIED_LOG = HOME / ".claude" / "evolve" / "applied.jsonl"
BACKUP_DIR = HOME / ".claude" / "evolve" / "backups"

# Only files matching one of these prefixes can be auto-modified.
TARGET_ALLOWLIST = [
    str(HOME / ".goosehints"),
    str(HOME / ".config" / "goose" / "skills"),
    str(HOME / ".claude" / "bin" / "presets"),
]

NEVER_MODIFY = {
    str(HOME / ".claude" / "settings.json"),
    str(HOME / ".claude" / "settings.local.json"),
    str(HOME / ".claude" / "bin" / "goose_dispatch.py"),
    str(HOME / ".claude" / "bin" / "auto_runtime.py"),
    str(HOME / ".claude" / "bin" / "evolve_run.py"),
    str(HOME / ".claude" / "bin" / "evolve_analyze.py"),
    str(HOME / ".claude" / "bin" / "evolve_apply.py"),
    str(HOME / ".claude" / "bin" / "evolve_extract.py"),
    str(HOME / ".claude" / "bin" / "evolve_fitness.py"),
}

DIFF_SIZE_CAP = 500


def load_proposals() -> list[dict]:
    if not PROPOSALS.exists():
        return []
    return [json.loads(line) for line in PROPOSALS.read_text().splitlines() if line.strip()]


def save_proposals(props: list[dict]) -> None:
    PROPOSALS.write_text("\n".join(json.dumps(p) for p in props) + "\n", encoding="utf-8")


def is_target_allowed(target: str) -> tuple[bool, str]:
    if target in NEVER_MODIFY:
        return False, "file is in NEVER_MODIFY"
    if not any(target == p or target.startswith(p + os.sep) for p in TARGET_ALLOWLIST):
        return False, "file not under any TARGET_ALLOWLIST prefix"
    return True, ""


def backup_file(target: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe = str(target).replace("/", "_").lstrip("_")
    dest = BACKUP_DIR / f"{ts}__{safe}"
    if target.exists():
        shutil.copy2(target, dest)
    return dest


def append_to_file(target: Path, content: str, anchor: str | None) -> bool:
    """Append content to target. Returns True if written, False if already present."""
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if content.strip() in existing:
        return False  # already present, idempotent
    sep = "\n" if existing and not existing.endswith("\n") else ""
    if anchor and anchor in existing:
        # Insert after the anchor line
        lines = existing.splitlines(keepends=True)
        out: list[str] = []
        inserted = False
        for line in lines:
            out.append(line)
            if not inserted and anchor in line:
                out.append(content.rstrip("\n") + "\n")
                inserted = True
        target.write_text("".join(out), encoding="utf-8")
    else:
        target.write_text(existing + sep + content.rstrip("\n") + "\n", encoding="utf-8")


def apply_proposal(prop: dict, dry_run: bool, force: bool) -> tuple[bool, str]:
    if prop.get("applied"):
        return False, "already applied"
    if not (prop.get("auto_apply") or force):
        return False, "auto_apply=false and --force not set"

    target = prop["target"]
    ok, reason = is_target_allowed(target)
    if not ok:
        return False, f"not allowed: {reason}"

    content = prop.get("content", "")
    if len(content) > DIFF_SIZE_CAP:
        return False, f"content exceeds DIFF_SIZE_CAP ({len(content)} > {DIFF_SIZE_CAP})"

    t = Path(target)

    if dry_run:
        return True, f"would append {len(content)} chars to {target}"

    # Don't snapshot a backup unless we're actually going to write.
    existing = t.read_text(encoding="utf-8") if t.exists() else ""
    if content.strip() in existing:
        return True, "no-op (content already present, marked applied)"
    backup = backup_file(t)
    append_to_file(t, content, prop.get("anchor"))
    return True, f"applied (backup: {backup.name})"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="Apply even proposals with auto_apply=false (still subject to safety)")
    p.add_argument("--id", default=None,
                   help="Apply only this proposal id (otherwise all unapplied)")
    args = p.parse_args()

    props = load_proposals()
    if args.id:
        props_to_consider = [p for p in props if p["id"] == args.id]
    else:
        props_to_consider = [p for p in props if not p.get("applied")]

    applied_count = 0
    skipped = []
    for prop in props_to_consider:
        ok, msg = apply_proposal(prop, args.dry_run, args.force)
        if ok and not args.dry_run:
            prop["applied"] = True
            prop["applied_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            applied_count += 1
            # Append to applied log
            APPLIED_LOG.parent.mkdir(parents=True, exist_ok=True)
            with APPLIED_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "proposal_id": prop["id"],
                    "applied_at": prop["applied_at"],
                    "target": prop["target"],
                    "content_preview": prop["content"][:120],
                    "result": msg,
                }) + "\n")
            print(f"APPLIED {prop['id']}: {msg} → {prop['target']}")
        elif ok and args.dry_run:
            print(f"DRY-RUN {prop['id']}: {msg}")
        else:
            skipped.append((prop["id"], msg))

    if not args.dry_run and applied_count > 0:
        # Persist updated proposals (applied flags changed)
        save_proposals(load_proposals() + [])  # reload to pick up other processes
        # Rewrite with our flags merged in
        # Simplest: re-save the updated list we have in memory
        all_props = load_proposals()
        id_map = {p["id"]: p for p in all_props}
        for p in props:
            if p.get("applied"):
                id_map[p["id"]]["applied"] = True
                id_map[p["id"]]["applied_at"] = p["applied_at"]
        save_proposals(list(id_map.values()))

    print(f"\napplied={applied_count} skipped={len(skipped)}")
    for pid, reason in skipped:
        print(f"  skip {pid}: {reason}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
