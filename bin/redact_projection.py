#!/usr/bin/env python3
"""Portable redactor for outbound external messages.

Closes CR-INV-007-PROJECTION-REDACTED (see
~/.claude/standards/CHAD_RUNTIME_INVARIANTS.md). Maps AgentOps's
INV-PROJECTION-REDACTED into the Chad runtime.

Strips Chad-internal markers from any string before it leaves the
machine — outbound Zoom messages, Slack posts, emails, public blog
posts. The redactor is intentionally conservative: it removes things
that are definitely internal-only (absolute /Users/ paths, named
internal agents, internal slash commands) and leaves everything else
alone.

The chadacus.dev professional-variant scrubber
(scripts/parse_findings.py) is the larger-grained version of this
pattern — it scrubs whole sentences flagged by Chad-perspective
markers. This module is the line/phrase-grained surface used by
runtime send paths.

CLI:
    cat draft.txt | redact_projection.py > scrubbed.txt
    redact_projection.py --strict < draft.txt   # exit 11 if any pattern hit

Python:
    from redact_projection import redact, was_redacted
    scrubbed = redact(text)
    if was_redacted(text, scrubbed):
        log.warning("Outbound message contained internal markers; scrubbed.")
"""

from __future__ import annotations

import argparse
import re
import sys

EXIT_OK = 0
EXIT_HIT = 11   # --strict: redaction occurred
EXIT_USAGE = 2

# Patterns are conservative. Order: most-specific first.
# Each tuple is (pattern, replacement). Replacement of "" deletes the match;
# otherwise the named replacement string is substituted.
DEFAULT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Absolute /Users/<name>/ paths — collapse to a generic placeholder
    (re.compile(r"/Users/[A-Za-z0-9_.-]+/[\S]*"), "<local-path>"),
    # ~/some/path  → <local-path>
    (re.compile(r"(?<!\w)~/[\w./\-]+"), "<local-path>"),
    # Internal agent names (named in CLAUDE.md / agents/)
    (re.compile(r"\bchad-(?:twin|agent|fleet)\b"), "<internal-agent>"),
    (re.compile(r"\bChad Twin\b|\bChad Agent\b"), "<internal-agent>"),
    # Internal-only slash commands. /pr, /commit, /help are NOT internal —
    # only the bespoke ones.
    (re.compile(r"/(?:govern|drive|build|build-backlog|orchestrate-local|alignment-grill|caveman|companion|codex-spar|codex-delegate|codex-branch|codex-security|evolve|rebecca-monitor|pokegen)\b"), "<internal-command>"),
    # Runtime artifact prefixes
    (re.compile(r"\b(?:auto_runtime|ralph_done_loop|completion_gate|pre_tool_guard|policy_edit_gate|edit_verify_async|hook_profile|notify_done|replan_evidence_check)\.py\b"), "<internal-script>"),
    # Internal hook event names — Chad-specific (not Claude Code spec events)
    (re.compile(r"\b(?:CR-INV-\d+)\b"), "<internal-rule-id>"),
    # Internal repos that are private
    (re.compile(r"\b(?:agentops-dogfood-lab|chad-fleet|cw-ai-kickstarter|chad-agent)\b"), "<internal-repo>"),
    # Sovereign log paths
    (re.compile(r"\.agentops/(?:runs|process-matrices|captaincy)/[\w./-]+"), "<sovereign-evidence>"),
]


def redact(text: str, *, patterns: list[tuple[re.Pattern, str]] | None = None) -> str:
    """Return `text` with internal markers replaced by neutral placeholders."""
    if not text:
        return text
    pats = patterns if patterns is not None else DEFAULT_PATTERNS
    out = text
    for pat, repl in pats:
        out = pat.sub(repl, out)
    return out


def was_redacted(original: str, scrubbed: str) -> bool:
    """True if `redact` changed `original`."""
    return original != scrubbed


def _cmd_main(args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    scrubbed = redact(raw)
    sys.stdout.write(scrubbed)
    if args.strict and was_redacted(raw, scrubbed):
        return EXIT_HIT
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Strip Chad-internal markers from outbound text. Reads stdin, writes stdout."
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 11 if any pattern was matched (caller can refuse to send).",
    )
    parser.set_defaults(func=_cmd_main)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
