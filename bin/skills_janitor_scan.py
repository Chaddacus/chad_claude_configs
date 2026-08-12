#!/usr/bin/env python3
"""Skills Janitor — scan transcripts for skill invocations and flag stale skills.

Walks ~/.claude/projects/*/*.jsonl, counts invocations of each Skill tool call
by skill name, compares against installed skills on disk, and prints a report.

Usage:
    python3 ~/.claude/bin/skills_janitor_scan.py [--days N] [--json]

Default window: 60 days. Skills with zero invocations in that window are flagged.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


HOME = Path.home()
PROJECTS_DIR = HOME / ".claude" / "projects"
SKILLS_DIR = HOME / ".claude" / "skills"
PLUGINS_DIR = HOME / ".claude" / "plugins"
SETTINGS_FILE = HOME / ".claude" / "settings.json"


def _enabled_plugins() -> set[str]:
    """Return the set of plugin IDs marked true in settings.json enabledPlugins.

    Returns empty set if settings.json is missing or malformed. If present,
    only plugins with value == True are included; False/missing are excluded.
    """
    try:
        with SETTINGS_FILE.open() as fh:
            settings = json.load(fh)
        ep = settings.get("enabledPlugins", {}) or {}
        return {name for name, enabled in ep.items() if enabled}
    except (OSError, json.JSONDecodeError):
        return set()


def parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def discover_installed_skills() -> set[str]:
    """Return the set of skill names actually loaded at session start.

    Local skills under ~/.claude/skills/ are always counted. Plugin skills
    are only counted if their source plugin is enabled in settings.json —
    disabled plugins are on disk but don't cost runtime tokens, so they
    shouldn't appear as "installed" from the perspective of this scan.
    """
    names = set()
    if SKILLS_DIR.is_dir():
        for child in SKILLS_DIR.iterdir():
            if child.is_dir() and (child / "SKILL.md").is_file():
                names.add(child.name)
    enabled = _enabled_plugins()
    marketplaces_root = PLUGINS_DIR / "marketplaces"
    if marketplaces_root.is_dir():
        for marketplace in marketplaces_root.iterdir():
            if not marketplace.is_dir():
                continue
            for plugin in marketplace.iterdir():
                if not plugin.is_dir():
                    continue
                # Plugin ID as referenced in settings.json.enabledPlugins
                plugin_id = f"{plugin.name}@{marketplace.name}"
                if plugin_id not in enabled:
                    continue
                skills_sub = plugin / "skills"
                if skills_sub.is_dir():
                    for child in skills_sub.iterdir():
                        if child.is_dir() and (child / "SKILL.md").is_file():
                            names.add(child.name)
    return names


import re

# Pattern to detect Read tool calls targeting a skill's SKILL.md file.
_SKILL_READ_RE = re.compile(r"skills/([^/]+)/SKILL\.md")
# Pattern to detect slash-command invocations in user prompts (e.g. "/drive", "/audit --fix").
_SLASH_CMD_RE = re.compile(r"(?:^|[\s])/([\w-]+)")


def scan_transcripts(since: datetime) -> tuple[dict[str, int], dict[str, datetime]]:
    """Return (invocation_counts, last_invocation_per_skill).

    Detects skill usage via three signals:
    1. Formal Skill tool invocations (tool_use with name="Skill")
    2. Read tool calls targeting skills/<name>/SKILL.md (direct file loading)
    3. Slash-command mentions in user prompts (e.g. "/drive", "/audit")
    """
    counts: dict[str, int] = defaultdict(int)
    last_seen: dict[str, datetime] = {}

    if not PROJECTS_DIR.is_dir():
        return counts, last_seen

    def _record(skill_name: str, ts: datetime) -> None:
        counts[skill_name] += 1
        if skill_name not in last_seen or ts > last_seen[skill_name]:
            last_seen[skill_name] = ts

    for project in PROJECTS_DIR.iterdir():
        if not project.is_dir():
            continue
        for jsonl in project.glob("*.jsonl"):
            try:
                with jsonl.open() as fh:
                    for line in fh:
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = parse_iso(rec.get("timestamp", ""))
                        if ts is None or ts < since:
                            continue
                        msg = rec.get("message", {})
                        role = msg.get("role", "")

                        # Signal 3: slash-command in user prompts
                        if role == "human":
                            for item in msg.get("content", []) or []:
                                text = ""
                                if isinstance(item, str):
                                    text = item
                                elif isinstance(item, dict) and item.get("type") == "text":
                                    text = item.get("text", "")
                                if text:
                                    for m in _SLASH_CMD_RE.finditer(text):
                                        _record(m.group(1), ts)

                        if role != "assistant":
                            continue
                        for item in msg.get("content", []) or []:
                            if item.get("type") != "tool_use":
                                continue
                            tool_name = item.get("name", "")
                            tool_input = item.get("input") or {}

                            # Signal 1: formal Skill tool invocation
                            if tool_name == "Skill":
                                skill_name = tool_input.get("skill")
                                if skill_name:
                                    _record(skill_name, ts)

                            # Signal 2: Read tool targeting a skill's SKILL.md
                            elif tool_name == "Read":
                                file_path = tool_input.get("file_path", "")
                                m = _SKILL_READ_RE.search(file_path)
                                if m:
                                    _record(m.group(1), ts)
            except OSError:
                continue

    return counts, last_seen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60, help="Lookback window (default: 60)")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    installed = discover_installed_skills()
    counts, last_seen = scan_transcripts(since)

    active = {name: counts[name] for name in installed if counts.get(name, 0) > 0}
    stale = sorted(installed - set(active.keys()))
    ghost_invocations = sorted(set(counts.keys()) - installed)

    report = {
        "window_days": args.days,
        "since": since.isoformat(),
        "installed_count": len(installed),
        "active_count": len(active),
        "stale_count": len(stale),
        "active": sorted(
            [(n, c, last_seen[n].isoformat()) for n, c in active.items()],
            key=lambda x: (-x[1], x[0]),
        ),
        "stale": stale,
        "ghost_invocations": ghost_invocations,
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"Skills Janitor — last {args.days} days")
    print(f"Installed: {report['installed_count']}  Active: {report['active_count']}  Stale: {report['stale_count']}")
    print()
    print("ACTIVE (name — count — last used):")
    for name, count, last in report["active"]:
        print(f"  {name:40s}  {count:4d}  {last[:10]}")
    print()
    if stale:
        print("STALE (no invocations in window — candidates for removal):")
        for name in stale:
            print(f"  {name}")
    else:
        print("STALE: none")
    print()
    if ghost_invocations:
        print("GHOST INVOCATIONS (skills invoked in transcripts but no longer installed):")
        for name in ghost_invocations:
            print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
