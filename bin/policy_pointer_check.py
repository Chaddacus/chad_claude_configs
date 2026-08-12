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

Scope is the policy surface DISCOVERED by glob (CLAUDE.md, standards/, rules/,
agents/, skills/*/SKILL.md) rather than a hand-maintained list — a hand list
goes stale the moment a doc is added, letting new docs carry dead pointers
indefinitely. Pass explicit paths as argv to check a subset.

Skips:
  - any path on a line carrying `<!-- pointer-check:skip -->`
  - every path under a heading whose text contains "legacy" or "not canonical"
  - paths with a VARIABLE component — glob (`state/*.jsonl`), template slot
    (`{track_id}`), shell expansion (`${sid}`, `$(date)`), or angle placeholder
    (`audit-<session_id>.jsonl`). The concrete parent directory is checked
    instead, so real rot under a bogus parent is still caught.

Use the skip marker for paths that intentionally do not resolve: command
OUTPUTS created on first run, BUILD artifacts, SECRETS files, illustrative
EXAMPLES, and refs a doc already documents as historically dead.

Exit 0 if all pointers resolve; exit 1 (listing the danglers) otherwise.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

HOME = Path.home()
CLAUDE_HOME = HOME / ".claude"

# The policy surface, discovered rather than hand-listed. The original four-doc
# list went stale the moment a standard or skill was added: a new doc could
# carry dead pointers indefinitely because nothing looked at it. Globbing means
# a doc is covered the day it lands.
_DOC_GLOBS = (
    "CLAUDE.md",
    "standards/*.md",
    "rules/*.md",
    "agents/*.md",
    "skills/*/SKILL.md",
    # Progressive disclosure moves load-bearing paths OUT of SKILL.md and into
    # references/. Without this glob, splitting a skill would silently shrink
    # pointer coverage — the reference file is exactly where a repo path or CLI
    # entrypoint ends up living.
    "skills/*/references/*.md",
)


def discover_docs() -> list[Path]:
    """Every policy doc the checker governs, sorted and de-duplicated."""
    found: set[Path] = set()
    for pattern in _DOC_GLOBS:
        found.update(p for p in CLAUDE_HOME.glob(pattern) if p.is_file())
    return sorted(found)


DEFAULT_DOCS = discover_docs()

SKIP_LINE_MARKER = "pointer-check:skip"
SKIP_HEADING_RE = re.compile(r"legacy|not canonical", re.IGNORECASE)
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")

# A pointer component that stands in for a value rather than naming one:
#   *          glob            (state/*.jsonl, ~/.ssh/id_*)
#   { }        template slot   ({track_id}, {YYYY-MM-DD}-{repo-slug})
#   $          shell expansion (${session_id}, $(date +%F), $PWD)
#   <          angle placeholder (stop_gate_audit-<session_id>.jsonl)
# Everything from the first such character on is unverifiable, so _resolve()
# falls back to the concrete directory in front of it.
_VARIABLE_RE = re.compile(r"[*{}$<]")

# path char class: stop at whitespace, backtick, markdown/paren delimiters, angle bracket
_PCHARS = r"[^\s`)\]\[<>,]"
ABS_RE = re.compile(rf"(?P<p>{re.escape(str(HOME))}/{_PCHARS}+)")
TILDE_RE = re.compile(rf"(?P<p>~/{_PCHARS}+)")
REL_RE = re.compile(
    rf"(?<![\w./])(?P<p>(?:standards|skills|bin|state|agents|rules|hooks)/{_PCHARS}+"
    r"\.(?:md|py|sh|json|toml|jsonl))"
)


# A `file:line` / `file:line:col` citation suffix. CLAUDE.md's review rules
# mandate citing "concrete file/line references", so this shape appears in any
# doc that follows policy; without stripping it the whole citation is treated
# as a filename and can never resolve.
_LINECOL_RE = re.compile(r":\d+(?::\d+)?$")


def _normalize(raw: str) -> str:
    """Strip trailing markdown/sentence punctuation a path regex may have captured.

    Quotes are included: a path quoted inside a shell snippet ("…/index.ts")
    otherwise keeps its closing quote and can never resolve. A trailing
    `:<line>[:<col>]` citation is stripped before the punctuation pass, so
    `bin/claude_run:157` is checked as `bin/claude_run`.
    """
    return _LINECOL_RE.sub("", raw.rstrip(".,;:\"'"))


def _resolve(raw: str, base: Path) -> Path:
    """Resolve a documented pointer to the most specific path that can exist.

    A pointer may carry a VARIABLE component that names no file on disk:
    a glob (`state/*.jsonl`), a template placeholder (`{track_id}`,
    `{YYYY-MM-DD}`), or a shell expansion (`${session_id}`, `$(date ...)`).
    All three mean "something goes here", so verifying the literal string is
    meaningless — verify the concrete directory that precedes it instead.

    Truncating at the wildcard is not enough on its own: `~/.ssh/id_*` becomes
    the prefix `~/.ssh/id_`, which is not a directory and never exists. Take the
    dirname unless the truncation already landed on a separator.
    """
    p = raw
    if p.startswith("~"):
        p = str(HOME) + p[1:]
    m = _VARIABLE_RE.search(p)
    if m:
        p = p[: m.start()]
        if not p.endswith(os.sep):
            p = os.path.dirname(p)
        if not p or p == str(HOME):
            # Nothing concrete left to verify (e.g. a bare "~/{placeholder}").
            return HOME
    return Path(p) if os.path.isabs(p) else base / p


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
                # _PCHARS stops the capture at `<`, so an angle-bracket
                # placeholder (`stop_gate_audit-<session_id>.jsonl`) would
                # otherwise survive as the truncated prefix
                # `stop_gate_audit-` and be reported as a dangling pointer.
                # Re-attach the delimiter so _resolve sees the variable part.
                if line[m.end():m.end() + 1] == "<":
                    raw += "<"
                if raw in seen:
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
