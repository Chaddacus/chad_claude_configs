"""Tests for policy_pointer_check — documented-pointer integrity.

The checker's job is to fail when a policy doc names a path that does not exist.
Two properties matter and pull against each other:

  1. It must still FAIL on real pointer rot (the positive control below). A
     checker that cannot fail is not evidence of anything.
  2. It must NOT fail on pointers carrying a variable component — globs,
     template slots, shell expansions. Those name no file by construction, so
     the concrete parent directory is what gets verified.

Property 2 is what makes it safe to point the checker at the whole policy
surface rather than four hand-listed docs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Direct import setup — add bin dir to sys.path so we can import the module
BIN_DIR = str(Path.home() / ".claude" / "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

import policy_pointer_check as ppc  # noqa: E402

HOME = Path.home()


def _doc(tmp_path, body: str) -> Path:
    """Write a throwaway policy doc and return its path."""
    p = tmp_path / "DOC.md"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Positive control — the checker MUST be able to fail
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dangling_pointer_is_reported(tmp_path):
    """A planted nonexistent path must be caught. Without this, every green
    run below is vacuous."""
    doc = _doc(tmp_path, "See `~/.claude/definitely-not-a-real-file-9f3c.md` for detail.\n")
    missing = ppc.check_doc(doc)
    assert len(missing) == 1
    assert "definitely-not-a-real-file-9f3c.md" in missing[0][1]


@pytest.mark.unit
def test_existing_pointer_is_not_reported(tmp_path):
    doc = _doc(tmp_path, "Policy lives at `~/.claude/CLAUDE.md`.\n")
    assert ppc.check_doc(doc) == []


# ---------------------------------------------------------------------------
# Variable components — glob / template / shell expansion
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_glob_resolves_to_containing_directory():
    """Regression: truncating at `*` yields the PREFIX `~/.ssh/id_`, which is
    not a directory and never exists. The dirname is what must be checked."""
    assert ppc._resolve("~/.ssh/id_*", HOME) == HOME / ".ssh"


@pytest.mark.unit
def test_glob_on_separator_keeps_directory():
    assert ppc._resolve("~/.claude/state/*.json", HOME) == HOME / ".claude" / "state"


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        "~/.claude/state/autonomy/{track_id}/objective.json",
        "~/.claude/state/cases/${session_id}/completion.json",
        "~/.claude/state/autonomy/{track_id}",
    ],
)
def test_template_slots_resolve_to_concrete_parent(raw):
    """{track_id} / ${session_id} name a value, not a file."""
    resolved = ppc._resolve(raw, HOME)
    assert resolved == HOME / ".claude" / "state" / "autonomy" or resolved == HOME / ".claude" / "state" / "cases"


@pytest.mark.unit
def test_shell_expansion_resolves_to_concrete_parent():
    """`$(date +%F)` appended to a filename stem — verify the directory."""
    assert ppc._resolve("~/.claude/state/telemetry.$(date +%F).json", HOME) == HOME / ".claude" / "state"


@pytest.mark.unit
def test_variable_pointers_do_not_fail_the_check(tmp_path):
    """End-to-end: a doc full of template paths under real dirs stays green."""
    doc = _doc(
        tmp_path,
        "Track state: `~/.claude/state/autonomy/{track_id}/objective.json`\n"
        "Case file: `~/.claude/state/cases/${session_id}/completion.json`\n"
        "Ledgers: `~/.claude/state/verify-ledgers/*.json`\n",
    )
    assert ppc.check_doc(doc) == []


@pytest.mark.unit
def test_variable_pointer_under_missing_directory_still_fails(tmp_path):
    """The variable rule must not become a blanket amnesty — if the concrete
    parent is itself bogus, that is real rot and must still be caught."""
    doc = _doc(tmp_path, "Bad: `~/.claude/no-such-dir-7a21/{track_id}/x.json`\n")
    missing = ppc.check_doc(doc)
    assert len(missing) == 1
    assert "no-such-dir-7a21" in missing[0][1]


# ---------------------------------------------------------------------------
# file:line citations
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "suffix",
    [":157", ":157:4", ":1", ":157.", ":157,"],
)
def test_line_citation_suffix_is_stripped(tmp_path, suffix):
    """CLAUDE.md's review rules mandate citing `file:line`, so the gate must
    resolve the file and ignore the locator. Before this, a policy-compliant
    citation was reported as a dangling pointer and blocked its own edit."""
    doc = _doc(tmp_path, f"See `~/.claude/bin/policy_pointer_check.py{suffix}` for the rule.\n")
    assert ppc.check_doc(doc) == []


@pytest.mark.unit
def test_line_citation_on_missing_file_still_fails(tmp_path):
    """Stripping the locator must not launder a dangling path (positive
    control: without this, the rule above could be 'ignore anything with a
    colon')."""
    doc = _doc(tmp_path, "See `~/.claude/bin/no-such-tool-9f31.py:157` for the rule.\n")
    missing = ppc.check_doc(doc)
    assert len(missing) == 1
    assert "no-such-tool-9f31.py" in missing[0][1]


@pytest.mark.unit
def test_port_like_suffix_is_not_a_path(tmp_path):
    """A trailing number that is NOT a line locator still resolves the stem;
    the gate cannot tell :8787 from :157 and must not try."""
    assert ppc._normalize("~/.claude/bin/serve.py:8787") == "~/.claude/bin/serve.py"


# ---------------------------------------------------------------------------
# Skip mechanisms
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_skip_marker_suppresses_line(tmp_path):
    doc = _doc(tmp_path, "Gone: `~/.claude/nope-4b12.md` <!-- pointer-check:skip -->\n")
    assert ppc.check_doc(doc) == []


@pytest.mark.unit
def test_legacy_heading_suppresses_section(tmp_path):
    doc = _doc(tmp_path, "## Legacy surfaces\n\nOld: `~/.claude/nope-4b12.md`\n")
    assert ppc.check_doc(doc) == []


@pytest.mark.unit
def test_angle_bracket_placeholder_ignored(tmp_path):
    doc = _doc(tmp_path, "Template: `~/.claude/agents/<name>.md`\n")
    assert ppc.check_doc(doc) == []


@pytest.mark.unit
def test_angle_placeholder_mid_filename_not_reported_as_prefix(tmp_path):
    """Regression: the path charclass stops at `<`, so
    `stop_gate_audit-<session_id>.jsonl` used to survive as the bare prefix
    `~/.claude/state/stop_gate_audit-` — a path that cannot exist — and was
    reported as rot. The earlier `<name>.md` case passed only because its
    capture happened to end on an existing DIRECTORY, so it never exercised
    this path."""
    doc = _doc(tmp_path, "Audit: `~/.claude/state/stop_gate_audit-<session_id>.jsonl`\n")
    assert ppc.check_doc(doc) == []


# ---------------------------------------------------------------------------
# The real surface must be clean
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_default_docs_all_resolve():
    """The checker's own default scope must pass. This is the gate that runs
    in anger; if it is red, policy docs point at files that do not exist."""
    failures = []
    for doc in ppc.DEFAULT_DOCS:
        if not doc.exists():
            failures.append(f"{doc} (doc itself missing)")
            continue
        for lineno, raw in ppc.check_doc(doc):
            failures.append(f"{doc}:{lineno} -> {raw}")
    assert not failures, "dangling pointers:\n" + "\n".join(failures)
