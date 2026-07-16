#!/usr/bin/env python3
"""
session_end_summary.py — SessionEnd hook that writes a mechanical summary of
the just-finished session to ~/.claude/state/session_summaries/.

Deterministic-by-design (Karpathy Rule 5):
  - File edits/creates: extracted from transcript Write/Edit/MultiEdit tool_use entries
  - Tests run: extracted from Bash tool_use entries matching pytest/cargo test/go test/jest
  - Notifications: extracted from notify_done.sh Bash entries
  - Background agents dispatched: counted from Task tool_use entries
  - omni-mem writes this session: queried by createdAt window
  - Git activity: optional, only if cwd is a git repo

NO LLM. The output is structured bullets + tables, not prose narrative.

Output: ~/.claude/state/session_summaries/<YYYY-MM-DD>-<short-session-id>.md
Side-effect: writes a single-line nudge to stderr with the file path so the
operator notices it landed.

Hook input contract (JSON via stdin, same shape as other hooks):
  {
    "session_id": "...",
    "transcript_path": "/.../session-jsonl",
    "cwd": "..."   (optional)
  }
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from omni_mem_route import container_for_cwd

SUMMARY_DIR = Path.home() / ".claude" / "state" / "session_summaries"

EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
TASK_TOOL = "Task"
BASH_TOOL = "Bash"

# Bash command patterns to flag as "tests run" or "notifications" etc.
TEST_RE = re.compile(
    r"\b("
    r"pytest|python\s+-m\s+pytest"
    r"|npm\s+(?:run\s+)?test"
    r"|yarn\s+test"
    r"|jest"
    r"|go\s+test"
    r"|cargo\s+test"
    r"|rspec|mocha|vitest"
    r")\b",
    re.IGNORECASE,
)
NOTIFY_RE = re.compile(r"notify_done\.sh", re.IGNORECASE)


def _read_input() -> dict:
    raw = sys.stdin.read()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _short_sid(sid: str) -> str:
    s = re.sub(r"[^A-Za-z0-9-]", "", sid)
    return s[:12] or "unknown"


def _iter_transcript(transcript_path: str):
    """Yield each parsed JSONL entry. Tolerant of malformed lines."""
    if not transcript_path:
        return
    p = Path(transcript_path).expanduser()
    if not p.exists() or not p.is_file():
        return
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except Exception:
        return


def _tool_uses(entry: dict):
    """Yield tool_use blocks from an entry (handles message-wrapped + bare shapes)."""
    msg = entry.get("message") if isinstance(entry.get("message"), dict) else entry
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block


def _user_messages(entry: dict) -> list[str]:
    msg = entry.get("message") if isinstance(entry.get("message"), dict) else entry
    if not isinstance(msg, dict):
        return []
    if msg.get("role") != "user":
        return []
    content = msg.get("content")
    out: list[str] = []
    if isinstance(content, str):
        if "<command-message>" not in content:
            out.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text") or ""
                if isinstance(t, str) and "<command-message>" not in t:
                    out.append(t)
    return out


def _scan_transcript(transcript_path: str) -> dict:
    """Return {edits: [(path, op)], tests: [cmd], notifies: [cmd], tasks: [desc], user_turn_count: int, first_user_msg: str|None}."""
    edits: list[tuple[str, str]] = []
    tests: list[str] = []
    notifies: list[str] = []
    tasks: list[str] = []
    user_turn_count = 0
    first_user_msg: str | None = None

    for entry in _iter_transcript(transcript_path):
        for msg in _user_messages(entry):
            user_turn_count += 1
            if first_user_msg is None:
                first_user_msg = msg.splitlines()[0][:200]
        for block in _tool_uses(entry):
            name = block.get("name", "")
            inp = block.get("input", {}) or {}
            if name in EDIT_TOOLS:
                fp = inp.get("file_path") or inp.get("notebook_path") or ""
                if isinstance(fp, str) and fp:
                    edits.append((fp, name))
            elif name == BASH_TOOL:
                cmd = inp.get("command") or ""
                if isinstance(cmd, str):
                    if TEST_RE.search(cmd):
                        tests.append(cmd.strip()[:180])
                    elif NOTIFY_RE.search(cmd):
                        notifies.append(cmd.strip()[:180])
            elif name == TASK_TOOL:
                desc = inp.get("description") or inp.get("subagent_type") or "subagent"
                if isinstance(desc, str):
                    tasks.append(desc.strip()[:120])

    return {
        "edits": edits,
        "tests": tests,
        "notifies": notifies,
        "tasks": tasks,
        "user_turn_count": user_turn_count,
        "first_user_msg": first_user_msg,
    }


def _git_summary(cwd: str | None) -> dict:
    """Return {branch, commits_since_session_start, changed_files}. Best-effort."""
    out: dict[str, object] = {"branch": None, "commits": [], "changed_files": []}
    if not cwd:
        return out
    cwd_path = Path(cwd).expanduser()
    if not (cwd_path / ".git").exists():
        # Try walking up
        cur = cwd_path
        for _ in range(4):
            if (cur / ".git").exists():
                cwd_path = cur
                break
            if cur.parent == cur:
                return out
            cur = cur.parent
        else:
            return out

    def _git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(cwd_path), *args],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except Exception:
            return ""

    out["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD") or None
    # Recent commits — last 10 across the session window. We don't know exact
    # session start; cap to last 10 since 24h to keep it bounded.
    commits = _git("log", "--since=24.hours", "--pretty=format:%h %s", "-n", "10")
    if commits:
        out["commits"] = [line for line in commits.splitlines() if line]
    changed = _git("status", "--porcelain")
    if changed:
        out["changed_files"] = [line.strip() for line in changed.splitlines() if line.strip()][:20]
    return out


def _omni_mem_writes_since(
    session_started_at: datetime.datetime | None, cwd: str | None = None
) -> dict:
    """Best-effort query of journal + recent saves. Returns {journal: [...], memories: [...]}.

    Routes to the work or personal omni-mem vault based on the session cwd
    (~/chad_personal -> omni-mem-personal, else omni-mem).
    """
    out: dict[str, list[str]] = {"journal": [], "memories": []}
    if session_started_at is None:
        return out

    workspace = os.environ.get("OMNI_MEM_WORKSPACE_ID", "chadsimon")
    container = container_for_cwd(cwd)

    def _docker_exec(*args: str) -> str:
        try:
            return subprocess.run(
                ["docker", "exec", container, "omni-mem", *args],
                capture_output=True,
                text=True,
                timeout=8,
            ).stdout
        except Exception:
            return ""

    # Journal: read recent entries, filter by createdAt > session_started_at
    raw = _docker_exec(
        "journal_read",
        "--workspaceId",
        workspace,
        "--agentName",
        "chad-twin",
        "--limit",
        "30",
    )
    try:
        data = json.loads(raw) if raw else []
        entries = data if isinstance(data, list) else data.get("entries", [])
        for e in entries:
            if not isinstance(e, dict):
                continue
            created = e.get("createdAt", "")
            try:
                ts = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:
                continue
            if ts >= session_started_at:
                topic = e.get("topic", "?")
                content = (e.get("content") or "")[:140]
                out["journal"].append(f"[{topic}] {content}")
    except Exception:
        pass

    # save_memory query — search by recent. Use search with empty query gets recent.
    raw = _docker_exec(
        "search",
        "--workspaceId",
        workspace,
        "--query",
        " ",  # broad
        "--limit",
        "30",
    )
    try:
        data = json.loads(raw) if raw else []
        results = data if isinstance(data, list) else data.get("results", [])
        for r in results:
            obs = r.get("observation", r) if isinstance(r, dict) else r
            if not isinstance(obs, dict):
                continue
            if obs.get("type") not in ("manual_note", "summary_note", None):
                pass
            created = obs.get("createdAt", "")
            try:
                ts = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:
                continue
            if ts >= session_started_at:
                title = obs.get("title", "?")
                out["memories"].append(title)
    except Exception:
        pass

    # Dedup, keep order
    seen: set[str] = set()
    out["journal"] = [x for x in out["journal"] if not (x in seen or seen.add(x))]
    seen.clear()
    out["memories"] = [x for x in out["memories"] if not (x in seen or seen.add(x))]
    return out


def _session_started_at(transcript_path: str) -> datetime.datetime | None:
    """Use the transcript's mtime of the first line as a proxy for session start."""
    if not transcript_path:
        return None
    p = Path(transcript_path).expanduser()
    if not p.exists():
        return None
    try:
        # Use file ctime as best-available session-start proxy
        return datetime.datetime.fromtimestamp(
            p.stat().st_ctime, tz=datetime.timezone.utc
        )
    except Exception:
        return None


def _format_summary(
    *,
    session_id: str,
    cwd: str | None,
    started_at: datetime.datetime | None,
    ended_at: datetime.datetime,
    transcript: dict,
    git: dict,
    memory: dict,
) -> str:
    lines: list[str] = []
    lines.append(f"# Session summary — {ended_at.strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append("")
    lines.append(f"- **session_id:** `{session_id}`")
    if started_at:
        lines.append(f"- **started:** {started_at.strftime('%Y-%m-%d %H:%M %Z')}")
        dur = ended_at - started_at
        h = dur.total_seconds() // 3600
        m = (dur.total_seconds() % 3600) // 60
        lines.append(f"- **duration:** {int(h)}h {int(m)}m")
    if cwd:
        lines.append(f"- **cwd:** `{cwd}`")
    lines.append(f"- **user turns:** {transcript['user_turn_count']}")
    if transcript.get("first_user_msg"):
        lines.append(f"- **opening prompt:** {transcript['first_user_msg']!r}")
    lines.append("")

    # Files edited
    edits = transcript["edits"]
    lines.append(f"## Files touched ({len(edits)} edits)")
    if not edits:
        lines.append("_None._")
    else:
        # Dedup by path, count ops
        by_path: dict[str, Counter] = {}
        for fp, op in edits:
            by_path.setdefault(fp, Counter())[op] += 1
        for fp, ops in sorted(by_path.items()):
            opstr = ", ".join(f"{k}×{v}" for k, v in ops.most_common())
            lines.append(f"- `{fp}` ({opstr})")
    lines.append("")

    # Tests
    tests = transcript["tests"]
    lines.append(f"## Tests run ({len(tests)})")
    if not tests:
        lines.append("_None detected._")
    else:
        for t in tests[:15]:
            lines.append(f"- `{t}`")
        if len(tests) > 15:
            lines.append(f"- _...and {len(tests) - 15} more._")
    lines.append("")

    # Subagent dispatches
    tasks = transcript["tasks"]
    if tasks:
        lines.append(f"## Subagents dispatched ({len(tasks)})")
        for t in tasks:
            lines.append(f"- {t}")
        lines.append("")

    # Notifications
    notifies = transcript["notifies"]
    if notifies:
        lines.append(f"## Notifications sent ({len(notifies)})")
        for n in notifies[:5]:
            lines.append(f"- `{n}`")
        if len(notifies) > 5:
            lines.append(f"- _...and {len(notifies) - 5} more._")
        lines.append("")

    # Git
    # Bind commits/cf unconditionally: the "no artifacts" check below reads
    # `commits` even when the Git block is skipped (clean/read-only session),
    # which previously raised UnboundLocalError.
    commits = git.get("commits") or []
    cf = git.get("changed_files") or []
    if git.get("branch") or commits or cf:
        lines.append("## Git")
        if git.get("branch"):
            lines.append(f"- **branch:** `{git['branch']}`")
        if commits:
            lines.append(f"- **commits in last 24h:** {len(commits)}")
            for c in commits[:10]:
                lines.append(f"  - {c}")
        if cf:
            lines.append(f"- **uncommitted changes:** {len(cf)}")
            for f in cf[:10]:
                lines.append(f"  - `{f}`")
        lines.append("")

    # Memory
    memories = memory.get("memories") or []
    if memories:
        lines.append(f"## omni-mem saves ({len(memories)})")
        for t in memories:
            lines.append(f"- {t}")
        lines.append("")

    journal = memory.get("journal") or []
    if journal:
        lines.append(f"## omni-mem journal entries ({len(journal)})")
        for j in journal:
            lines.append(f"- {j}")
        lines.append("")

    if not (edits or tests or tasks or commits or memories or journal):
        lines.append("## Notes")
        lines.append("_Session produced no detectable artifacts; likely a read-only / Q&A session._")
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    raw = _read_input()
    session_id = str(raw.get("session_id", "unknown"))
    transcript_path = str(raw.get("transcript_path", "") or "").replace("~", str(Path.home()))
    cwd = raw.get("cwd")

    started_at = _session_started_at(transcript_path)
    ended_at = datetime.datetime.now(tz=datetime.timezone.utc)

    transcript = _scan_transcript(transcript_path)
    git = _git_summary(cwd if isinstance(cwd, str) else None)
    memory = _omni_mem_writes_since(started_at, cwd if isinstance(cwd, str) else None)

    summary = _format_summary(
        session_id=session_id,
        cwd=cwd if isinstance(cwd, str) else None,
        started_at=started_at,
        ended_at=ended_at,
        transcript=transcript,
        git=git,
        memory=memory,
    )

    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    date = ended_at.strftime("%Y-%m-%d")
    out_path = SUMMARY_DIR / f"{date}-{_short_sid(session_id)}.md"
    out_path.write_text(summary, encoding="utf-8")

    # Single stderr nudge so the operator notices it landed.
    print(f"[session-summary] {out_path}", file=sys.stderr)

    # Hook contract: emit {} (no decision needed for SessionEnd).
    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
