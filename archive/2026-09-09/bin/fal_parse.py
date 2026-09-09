#!/usr/bin/env python3
"""Failure Abstraction Layer (FAL) parser — SAFEdit-pattern.

Stage 1: deterministic regex extraction of test name, exception type, expected/actual, file:line.
Stage 2: pattern-matching classification to leaf failure types and root causes.
Stage 3: prose diagnosis + suggested action + confidence score.

NO LLM CALLS. Pure pattern matching, fully reproducible, near-zero cost.

Reference: arXiv:2604.25737 (SAFEdit).

Usage:
  cat raw_test_output.txt | fal_parse.py
  fal_parse.py --from path/to/output.txt
  fal_parse.py --from path/to/output.txt --tool-use-id <id>

Output: JSON failure record on stdout.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any

# ---------------------------------------------------------------------------
# Stage 2 classification tables
# ---------------------------------------------------------------------------

# Leaf failure types -> root causes.
# Started with 14 in the paper; reduced to 8 + UNKNOWN for our scale.
LEAF_TO_ROOT: dict[str, str] = {
    "ASSERTION_MISMATCH": "Implementation Gap",
    "SYNTAX_ERROR":       "Implementation Gap",
    "TYPE_ERROR":         "Implementation Gap",
    "VALUE_ERROR":        "Implementation Gap",
    "ATTRIBUTE_ERROR":    "Context Misalignment",
    "NAME_ERROR":         "Context Misalignment",
    "IMPORT_ERROR":       "Context Misalignment",
    "TIMEOUT":            "Implementation Gap",
    "UNKNOWN":            "Implementation Gap",
}

ACTION_TEMPLATES: dict[str, str] = {
    "ASSERTION_MISMATCH": "Modify the logic so the produced value matches the expected one.",
    "SYNTAX_ERROR":       "Fix the syntax error at the indicated location.",
    "TYPE_ERROR":         "Reconcile the operand types or convert before use.",
    "VALUE_ERROR":        "Validate inputs or adjust the value range to satisfy the call site.",
    "ATTRIBUTE_ERROR":    "Reference the correct attribute, or rename the call site to match the new identifier.",
    "NAME_ERROR":         "Define or import the missing identifier; check for a typo.",
    "IMPORT_ERROR":       "Install the missing module, or correct the import path.",
    "TIMEOUT":            "Audit for infinite loops or runaway recursion; add a deterministic exit condition.",
    "UNKNOWN":            "Inspect raw_excerpt; the failure shape did not match a known pattern.",
}

# ---------------------------------------------------------------------------
# Stage 1 regex extractors
# ---------------------------------------------------------------------------

# pytest summary line: "FAILED <path>::<test> - <Exception>: <msg>"
# (`-` separator distinguishes from the inline progress lines like "<path>::<test> FAILED [ 33%]")
_RE_PYTEST_FAILED   = re.compile(r"^FAILED\s+([^\s]+::[^\s]+)", re.MULTILINE)
_RE_TEST_FN         = re.compile(r"\b(test_[A-Za-z0-9_]+)\b")
# Allow pytest's "E       " prefix and common indentation before the exception line.
_RE_PY_EXCEPTION    = re.compile(
    r"^(?:E\s+|\s*)(\w*Error|\w*Exception)(?::\s*(.*))?$",
    re.MULTILINE,
)
_RE_FILE_LINE_PY    = re.compile(r'File "([^"]+)", line (\d+)')
_RE_FILE_LINE_GENERIC = re.compile(r"([\w./\-]+\.[a-zA-Z]+):(\d+)")
# AssertionError: assert <actual> == <expected>     (pytest's standard shape — left=actual, right=expected)
_RE_ASSERT_PYTEST   = re.compile(
    r"AssertionError:\s*assert\s+(.+?)\s*==\s*(.+?)\s*$",
    re.MULTILINE,
)
# Bare assertion fallback (no AssertionError wrapper).
_RE_ASSERT_BARE     = re.compile(r"^\s*assert\s+(.+?)\s*==\s*(.+?)\s*$", re.MULTILINE)
_RE_RUST_TEST_FAIL  = re.compile(r"test\s+([\w:]+)\s+\.\.\.\s+FAILED")
_RE_GO_TEST_FAIL    = re.compile(r"--- FAIL:\s+(\w+)")
_RE_TSC_ERROR       = re.compile(r"(?:error\s+TS\d+|TypeError):\s*(.+)")
_RE_TIMEOUT_HINTS   = re.compile(r"\b(?:timeout|timed out|deadline exceeded)\b", re.IGNORECASE)


def _first(pattern: re.Pattern[str], text: str, group: int = 1) -> str:
    m = pattern.search(text)
    if not m:
        return ""
    try:
        return (m.group(group) or "").strip()
    except IndexError:
        return ""


def stage1_extract(raw: str) -> dict[str, str]:
    """Pull test name, exception type, expected/actual, file:line from raw output."""
    out: dict[str, str] = {
        "test": "",
        "exception": "",
        "exception_message": "",
        "expected": "",
        "actual": "",
        "file": "",
        "line": "",
    }

    # test name — try multiple frameworks
    for pat in (_RE_PYTEST_FAILED, _RE_RUST_TEST_FAIL, _RE_GO_TEST_FAIL):
        name = _first(pat, raw)
        if name:
            out["test"] = name
            break
    if not out["test"]:
        out["test"] = _first(_RE_TEST_FN, raw)

    # exception type + message
    m = _RE_PY_EXCEPTION.search(raw)
    if m:
        out["exception"] = (m.group(1) or "").strip()
        out["exception_message"] = (m.group(2) or "").strip()

    # expected/actual via AssertionError (pytest convention: left=actual, right=expected)
    m = _RE_ASSERT_PYTEST.search(raw)
    if m:
        out["actual"] = (m.group(1) or "").strip()
        out["expected"] = (m.group(2) or "").strip()
    if not out["expected"]:
        m = _RE_ASSERT_BARE.search(raw)
        if m:
            out["actual"] = (m.group(1) or "").strip()
            out["expected"] = (m.group(2) or "").strip()

    # file:line — prefer Python traceback shape, then generic
    m = _RE_FILE_LINE_PY.search(raw)
    if m:
        out["file"] = m.group(1)
        out["line"] = m.group(2)
    else:
        m = _RE_FILE_LINE_GENERIC.search(raw)
        if m:
            out["file"] = m.group(1)
            out["line"] = m.group(2)

    return out


# ---------------------------------------------------------------------------
# Stage 2 classification
# ---------------------------------------------------------------------------

def stage2_classify(stage1: dict[str, str], raw: str) -> str:
    """Map extracted signal to one of the leaf types."""
    exc = stage1.get("exception", "")
    msg = stage1.get("exception_message", "")
    combined = f"{exc} {msg}".lower()

    if exc == "AssertionError" or stage1.get("expected") and stage1.get("actual"):
        return "ASSERTION_MISMATCH"
    if exc == "SyntaxError" or "syntaxerror" in combined or "unexpected token" in combined:
        return "SYNTAX_ERROR"
    if exc == "ModuleNotFoundError" or exc == "ImportError" or "no module named" in combined:
        return "IMPORT_ERROR"
    if exc == "AttributeError" or "has no attribute" in combined:
        return "ATTRIBUTE_ERROR"
    if exc == "NameError" or "is not defined" in combined:
        return "NAME_ERROR"
    if exc == "TypeError":
        return "TYPE_ERROR"
    if exc == "ValueError":
        return "VALUE_ERROR"
    if _RE_TIMEOUT_HINTS.search(raw):
        return "TIMEOUT"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Stage 3 prose + confidence
# ---------------------------------------------------------------------------

def stage3_prose(stage1: dict[str, str], leaf_type: str) -> tuple[str, str]:
    """Return (diagnosis, action) strings."""
    expected = stage1.get("expected", "")
    actual = stage1.get("actual", "")
    if leaf_type == "ASSERTION_MISMATCH" and expected and actual:
        diagnosis = f"Test asserted {expected!r} but got {actual!r}."
    elif leaf_type == "ASSERTION_MISMATCH":
        diagnosis = "Assertion failed; expected/actual values were not extractable."
    elif leaf_type == "TIMEOUT":
        diagnosis = "Process exceeded its time budget — likely infinite loop, runaway recursion, or a blocking call without a deadline."
    elif stage1.get("exception_message"):
        diagnosis = f"{stage1['exception']}: {stage1['exception_message']}"
    elif stage1.get("exception"):
        diagnosis = f"{stage1['exception']} raised."
    elif leaf_type == "UNKNOWN":
        diagnosis = "Failure shape did not match a known pattern; inspect raw_excerpt."
    else:
        diagnosis = f"{leaf_type} detected via heuristic; structured fields not extractable."
    action = ACTION_TEMPLATES.get(leaf_type, ACTION_TEMPLATES["UNKNOWN"])
    return diagnosis, action


def confidence(stage1: dict[str, str], leaf_type: str) -> float:
    """Cheap confidence heuristic — share of stage1 fields successfully extracted."""
    if leaf_type == "UNKNOWN":
        return 0.2
    if stage1.get("test") and stage1.get("exception") and (stage1.get("expected") or stage1.get("file")):
        return 0.95
    if stage1.get("test") and stage1.get("exception"):
        return 0.7
    if stage1.get("exception"):
        return 0.4
    return 0.2


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def parse_fal(raw: str, tool_use_id: str = "") -> dict[str, Any]:
    s1 = stage1_extract(raw)
    leaf = stage2_classify(s1, raw)
    diag, action = stage3_prose(s1, leaf)
    return {
        "ts": time.time(),
        "tool_use_id": tool_use_id,
        "test": s1["test"],
        "leaf_type": leaf,
        "root_cause": LEAF_TO_ROOT.get(leaf, "Implementation Gap"),
        "diagnosis": diag,
        "action": action,
        "expected": s1["expected"],
        "actual": s1["actual"],
        "exception": s1["exception"],
        "exception_message": s1["exception_message"],
        "file": s1["file"],
        "line": s1["line"],
        "confidence": confidence(s1, leaf),
        "raw_excerpt": raw[-2000:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", help="Path to raw output file (default: stdin)")
    ap.add_argument("--tool-use-id", default="", help="Optional tool_use_id to embed in record")
    args = ap.parse_args()

    if args.src:
        try:
            with open(args.src, encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except Exception as exc:
            sys.stderr.write(f"fal_parse: cannot read {args.src}: {exc}\n")
            return 2
    else:
        raw = sys.stdin.read()

    rec = parse_fal(raw, tool_use_id=args.tool_use_id)
    sys.stdout.write(json.dumps(rec, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
