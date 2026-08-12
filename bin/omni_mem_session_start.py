#!/usr/bin/env python3
"""SessionStart hook — inject omni-mem briefing for the current workspace.

Runs on session startup (and optionally resume). Derives the workspace ID
from $PWD basename, calls omni-mem's build_briefing tool via MCP stdio, and
emits hookSpecificOutput.additionalContext for Claude Code to inject into
the session.

Fails silently on any error — never blocks session start.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TIMEOUT_SECONDS = 5
# Container is selected by cwd: ~/chad_personal -> personal vault, else work vault.
from omni_mem_route import container_for_cwd

CONTAINER = container_for_cwd()


def _workspace_id() -> str:
    """Derive workspace ID from CWD basename, falling back to 'default'."""
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    name = Path(cwd).name
    return name or "default"


def _mcp_call(workspace_id: str) -> str | None:
    """One-shot MCP call to omni-mem's build_briefing. Returns text or None."""
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "session-start-hook", "version": "0"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "build_briefing",
            "arguments": {"workspaceId": workspace_id},
        },
    }
    payload = "\n".join(json.dumps(m) for m in (init, initialized, call)) + "\n"

    try:
        proc = subprocess.run(
            ["docker", "exec", "-i", CONTAINER, "omni-mem", "mcp-server"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    for line in proc.stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") != 2:
            continue
        result = msg.get("result") or {}
        content = result.get("content") or []
        texts = [c.get("text") for c in content if c.get("type") == "text" and c.get("text")]
        if texts:
            return "\n".join(texts)
        structured = result.get("structuredContent")
        if structured:
            return json.dumps(structured, indent=2)
    return None


MAX_CHARS = 4000

# This block arrives before any grounding has happened, so it must not read as
# established fact. rules/ai-engineering.md: memory is non-authoritative unless
# explicitly designed otherwise, with provenance where consequential.
# rules/execution-orchestration.md: memory is a claim, not current truth.
CLAIM_NOTICE = (
    "Recalled memory, not current truth. Each line is a claim recorded at the "
    "time shown, and the system it describes may have changed since. Verify "
    "against the repository, the live service or git before acting on one."
)

# Rendered in this order. Observations and facts lead because they are the
# workspace's own recorded statements; topics are a frequency index and lose
# least by being cut.
SECTION_ORDER = (
    ("recentObservations", "Recent observations", "_render_observation"),
    ("activeFacts", "Active facts", "_render_fact"),
    ("synthesisPages", "Synthesis pages", "_render_page"),
    ("identityFacts", "Identity facts", "_render_fact"),
    ("preferences", "Preferences", "_render_fact"),
    ("relevantTopics", "Topics", "_render_topic"),
)


def _trim(text: str, limit: int) -> str:
    """Shorten to `limit` chars at a space boundary, marking that it was cut.

    A silent cut reads as a complete statement, so the marker is part of the
    contract, not decoration.
    """
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + " […]"


def _parse_time(value) -> datetime | None:
    """Read one of omni-mem's ISO timestamps, or give up quietly.

    They arrive Zulu-suffixed ("2026-08-12T04:54:27.891Z"), which
    datetime.fromisoformat does not accept before Python 3.11.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age(item: dict, reference: datetime | None) -> str:
    """Age of a record as a short relative token, e.g. "3d" or "5h".

    Relative, not absolute: the question a reader has about a remembered claim
    is how stale it is, and a date makes them do that subtraction themselves.
    Returns "" when either end is unreadable — a wrong age is worse than none.
    """
    created = _parse_time(item.get("createdAt") or item.get("updatedAt"))
    if created is None or reference is None:
        return ""
    try:
        delta = reference - created
    except TypeError:  # one side naive, one side aware
        return ""
    seconds = delta.total_seconds()
    if seconds < 0:
        return ""
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _provenance(parts: list) -> str:
    """Join the non-empty provenance tokens into a compact suffix."""
    kept = [str(p) for p in parts if p]
    return (" · " + " · ".join(kept)) if kept else ""


def _render_observation(item: dict, limit: int, reference: datetime | None = None) -> str | None:
    title = str(item.get("title") or "").strip()
    body = str(item.get("text") or "").strip()
    if not title and not body:
        return None
    origin = str(item.get("agentFamily") or item.get("source") or "").strip()
    meta = _provenance([_age(item, reference), origin and f"via {origin}"])
    head = f"- **{title or '(untitled)'}**{meta}"
    if not body:
        return head
    return head + "\n  " + _trim(body, max(80, limit - len(head)))


def _render_fact(item: dict, limit: int, reference: datetime | None = None) -> str | None:
    """Facts arrive as subject/predicate/object triples, or as plain statements.

    build_briefing carries `subjectId` as an opaque `fact-entity:<hex>` handle
    and ships no name for it, so the predicate alone reads as a statement with
    a missing referent ("are enforced as ..."). The handle is shown rather than
    dropped: an unresolved subject is visible, and a reader cannot misattribute
    the claim to whatever subject the previous line had.

    identityFacts and preferences were empty on every workspace measured, so
    they are rendered through the same triple reader with a text fallback
    rather than given a shape this code has never actually seen.
    """
    confidence = item.get("confidence")
    meta = _provenance([
        _age(item, reference),
        f"confidence {confidence}" if isinstance(confidence, (int, float)) else "",
    ])
    predicate = str(item.get("predicate") or "").strip()
    obj = str(item.get("object") or "").strip()
    if predicate or obj:
        subject = str(item.get("subjectId") or "").split(":")[-1][:8]
        statement = " ".join(p for p in (predicate, obj) if p)
        prefix = f"(subject {subject}) " if subject else ""
        budget = max(80, limit - len(prefix) - len(meta))
        return "- " + prefix + _trim(statement, budget) + meta
    for field in ("text", "value", "title", "label"):
        got = item.get(field)
        if isinstance(got, str) and got.strip():
            return "- " + _trim(got, max(80, limit - len(meta))) + meta
    return None


def _render_page(item: dict, limit: int, reference: datetime | None = None) -> str | None:
    title = str(item.get("title") or "").strip()
    body = str(item.get("body") or "").strip()
    if not title and not body:
        return None
    trust = item.get("trustLevel")
    citations = item.get("citations")
    meta = _provenance([
        _age(item, reference),
        f"trust {trust}" if isinstance(trust, (int, float)) else "",
        f"{len(citations)} citations" if isinstance(citations, list) and citations else "",
    ])
    head = f"- **{title or '(untitled)'}**{meta}"
    if not body:
        return head
    return head + "\n  " + _trim(body, max(80, limit - len(head)))


def _render_topic(item: dict, limit: int, reference: datetime | None = None) -> str | None:
    label = str(item.get("label") or item.get("topicKey") or "").strip()
    if not label:
        return None
    count = item.get("count")
    return "- " + _trim(f"{label} ({count})" if count is not None else label, limit)


def _sections(data: dict) -> list[tuple[str, list, object]]:
    """Non-empty sections in render order, paired with their item renderer."""
    out = []
    for key, heading, renderer_name in SECTION_ORDER:
        items = data.get(key)
        if isinstance(items, list) and items:
            out.append((heading, items, globals()[renderer_name]))
    return out


def render_briefing(data: dict, max_chars: int = MAX_CHARS) -> str | None:
    """Render a build_briefing payload as budgeted markdown.

    The previous behaviour cut the raw JSON at a fixed character count. Because
    JSON serialises its sections in a fixed order, that cut did not sample the
    briefing — it deleted every section after the first one or two, in full, on
    every session. Measured on workspace chad_work: 25,131 chars produced, 4,000
    kept, and `activeFacts`, `synthesisPages`, `relevantTopics` and `summary`
    never reached the session at all.

    Three guarantees hold here, each verified by mutating it away and watching
    a specific test die (tests/test_omni_mem_session_start.py):

    1. Every non-empty section renders at least one item, so no section can
       disappear in silence. This outranks the budget: a section whose first
       item is larger than its whole share is still shown, trimmed.
    2. Each section reserves a share of the budget for the sections that follow
       it, so a long leading section cannot spend the whole allowance. Total
       output therefore stays near `max_chars`, exceeding it only by the
       overspill guarantee 1 allows — bounded by one item per section.
    3. Every drop is stated, in the heading or with a trim marker.

    Each line also carries its own provenance — age, and whichever of origin,
    confidence or trust the record has — so a reader can weigh one claim
    against another. `main` states the claim status once for the whole block;
    this states the standing of each item inside it. Ages are measured against
    the payload's own `generatedAt` so they do not drift with the clock of
    whatever later reads it.

    Returns None when the payload has no renderable section, which keeps the
    hook silent instead of injecting an empty heading.
    """
    if not isinstance(data, dict):
        return None
    sections = _sections(data)
    if not sections:
        return None

    reference = _parse_time(data.get("generatedAt")) or datetime.now(timezone.utc)
    share = max(1, max_chars // len(sections))
    lines: list[str] = []
    spent = 0

    for index, (heading, items, renderer) in enumerate(sections):
        remaining_sections = len(sections) - index
        # Unspent budget flows forward, but never into the shares still owed to
        # the sections after this one. The max() is the recovery path: an
        # earlier section may overspend by one item under guarantee 1, and this
        # stops that overspend cascading into a zero allowance here.
        reserved = share * (remaining_sections - 1)
        allowance = max(share, max_chars - spent - reserved)
        # Spread a section's allowance over its first few items rather than
        # letting one long record consume the section.
        per_item = max(1, allowance // max(1, min(len(items), 6)))

        block: list[str] = []
        used = 0
        shown = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            rendered = renderer(item, per_item, reference)
            if not rendered:
                continue
            if used + len(rendered) > allowance and shown:
                break
            block.append(rendered)
            used += len(rendered) + 1
            shown += 1
        if not block:
            continue

        dropped = len(items) - shown
        title = f"### {heading} ({len(items)})" if not dropped else \
                f"### {heading} — showing {shown} of {len(items)}"
        lines.append(title)
        lines.extend(block)
        lines.append("")
        spent += len(title) + used + 1

    if not lines:
        return None
    return "\n".join(lines).rstrip()


def main() -> int:
    try:
        _ = sys.stdin.read()  # consume hook input; we don't need its fields
    except Exception:
        pass

    workspace_id = _workspace_id()
    briefing = _mcp_call(workspace_id)
    if not briefing:
        return 0

    # The hook must never block session start, so a payload that is not the
    # JSON this renderer expects falls back to the original raw-and-cut path
    # rather than raising.
    rendered = None
    try:
        rendered = render_briefing(json.loads(briefing), MAX_CHARS)
    except (json.JSONDecodeError, TypeError, ValueError):
        rendered = None

    if rendered is not None:
        briefing = rendered
    elif len(briefing) > MAX_CHARS:
        briefing = briefing[:MAX_CHARS] + "\n\n[briefing truncated]"

    # The notice sits on both paths deliberately. The fallback is the one that
    # needs it most: it injects the raw payload, which reads as a data dump and
    # carries no per-item standing at all.
    header = (
        f"## omni-mem briefing — workspace `{workspace_id}`\n\n"
        f"{CLAIM_NOTICE}\n\n"
    )
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": header + briefing,
        }
    }
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
