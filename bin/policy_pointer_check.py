#!/usr/bin/env python3
"""Policy pointer integrity check.

Replaces the Codex-era `check_codex_policy_consistency.py` for `~/.claude` policy
edits. Scope is deliberately narrow: every concrete filesystem pointer named in a
policy doc must resolve on disk. This catches the pointer-rot class (dead workspace
refs, moved standards docs, broken stub destinations) without the brittle
index-sync machinery of the old checker.

Checks three pointer shapes inside the given docs:
  A. absolute paths under the home dir   (/Users/<user>/...)
  B. tilde paths                          (~/...)
  C. relative policy-dir paths            (standards|skills|bin|state|agents|rules|hooks)/...
     resolved against ~/.claude  (covers CLAUDE.md stub destinations)

Skips:
  - any path on a line carrying `<!-- pointer-check:skip -->`
  - every path under a heading whose text contains "legacy" or "not canonical"
  - template paths containing `<...>` placeholders (e.g. <repo>, <session>)
  - glob paths: the pre-`*` directory is checked instead

Exit 0 if all pointers resolve; exit 1 (listing the danglers) otherwise.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

HOME = Path.home()
CLAUDE_HOME = HOME / ".claude"

DEFAULT_DOCS = [
    CLAUDE_HOME / "CLAUDE.md",
    CLAUDE_HOME / "standards" / "REFERENCE_INDEX.md",
    CLAUDE_HOME / "standards" / "POLICY_OWNERSHIP.md",
    CLAUDE_HOME / "rules" / "policy-files.md",
]

SKIP_LINE_MARKER = "pointer-check:skip"
SKIP_HEADING_RE = re.compile(r"legacy|not canonical", re.IGNORECASE)
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")

# path char class: stop at whitespace, backtick, markdown/paren delimiters, angle bracket
_PCHARS = r"[^\s`)\]\[<>,]"
ABS_RE = re.compile(rf"(?P<p>{re.escape(str(HOME))}/{_PCHARS}+)")
TILDE_RE = re.compile(rf"(?P<p>~/{_PCHARS}+)")
REL_RE = re.compile(
    rf"(?<![\w./])(?P<p>(?:standards|skills|bin|state|agents|rules|hooks)/{_PCHARS}+"
    r"\.(?:md|py|sh|json|toml|jsonl))"
)


def _normalize(raw: str) -> str:
    """Strip trailing markdown/sentence punctuation a path regex may have captured."""
    return raw.rstrip(".,;:")


def _resolve(raw: str, base: Path) -> Path:
    p = raw
    if p.startswith("~"):
        p = str(HOME) + p[1:]
    candidate = Path(p) if os.path.isabs(p) else base / p
    # glob component: check the directory that precedes the first wildcard
    s = str(candidate)
    if "*" in s:
        candidate = Path(s.split("*", 1)[0])
    return candidate


def check_doc(doc: Path) -> list[tuple[int, str]]:
    """Return [(lineno, raw_pointer), ...] for pointers that do not resolve."""
    missing: list[tuple[int, str]] = []
    skip_section = False
    for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
        heading = HEADING_RE.match(line)
        if heading:
            skip_section = bool(SKIP_HEADING_RE.search(heading.group(1)))
            continue
        if skip_section or SKIP_LINE_MARKER in line:
            continue
        seen: set[str] = set()
        for rx, base in ((ABS_RE, None), (TILDE_RE, None), (REL_RE, CLAUDE_HOME)):
            for m in rx.finditer(line):
                raw = _normalize(m.group("p"))
                if "<" in raw or ">" in raw or raw in seen:
                    continue
                seen.add(raw)
                target = _resolve(raw, base or doc.parent)
                if not target.exists():
                    missing.append((lineno, raw))
    return missing


def main(argv: list[str]) -> int:
    docs = [Path(a) for a in argv] if argv else DEFAULT_DOCS
    total_missing = 0
    for doc in docs:
        if not doc.exists():
            print(f"DANGLING DOC: {doc} (policy doc itself is missing)")
            total_missing += 1
            continue
        for lineno, raw in check_doc(doc):
            print(f"DANGLING POINTER: {doc}:{lineno} -> {raw}")
            total_missing += 1
    if total_missing:
        print(f"\nFAIL: {total_missing} dangling pointer(s).")
        return 1
    print(f"OK: all canonical pointers resolve across {len(docs)} policy doc(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
