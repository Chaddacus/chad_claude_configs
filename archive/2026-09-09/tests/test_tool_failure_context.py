"""Tests for tool_failure_context.py hook."""

import json
import sys
from pathlib import Path

import pytest

# Import target functions directly for unit tests
sys.path.insert(0, str(Path.home() / ".claude" / "bin"))
from tool_failure_context import classify_failure, extract_errors, classify_introduced

from conftest import TOOL_FAILURE_CONTEXT


# ---------------------------------------------------------------------------
# Failure classification (classify_failure)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_npm_test():
    assert classify_failure("npm test") == "test"


@pytest.mark.unit
def test_pytest():
    assert classify_failure("pytest -v") == "test"


@pytest.mark.unit
def test_npx_tsc():
    assert classify_failure("npx tsc --noEmit") == "type"


@pytest.mark.unit
def test_cargo_check():
    assert classify_failure("cargo check") == "type"


@pytest.mark.unit
def test_npm_build():
    assert classify_failure("npm run build") == "build"


@pytest.mark.unit
def test_make():
    assert classify_failure("make") == "build"


@pytest.mark.unit
def test_curl():
    assert classify_failure("curl example.com") == "unknown"


# ---------------------------------------------------------------------------
# Error extraction (extract_errors)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ts_error():
    output = "app.tsx(15,3): error TS2322: Type 'string' not assignable"
    errors = extract_errors(output, "type")
    assert len(errors) == 1
    assert errors[0]["file"] == "app.tsx"
    assert errors[0]["line"] == "15"


@pytest.mark.unit
def test_pytest_failed():
    output = "FAILED tests/test_x.py::test_foo - AssertionError"
    errors = extract_errors(output, "test")
    assert len(errors) == 1
    assert errors[0]["file"] is not None


@pytest.mark.unit
def test_jest_error():
    output = "\u25cf should render correctly"
    errors = extract_errors(output, "test")
    assert len(errors) == 1
    assert "should render correctly" in errors[0]["message"]


@pytest.mark.unit
def test_go_error():
    output = "main.go:25:10: undefined: Foo"
    errors = extract_errors(output, "type")
    assert len(errors) == 1
    assert errors[0]["file"] == "main.go"
    assert errors[0]["line"] == "25"


@pytest.mark.unit
def test_generic_fallback():
    output = "Error: something went wrong"
    errors = extract_errors(output, "unknown")
    assert len(errors) == 1


@pytest.mark.unit
def test_max_5_errors():
    output = "\n".join(f"Error: problem {i}" for i in range(10))
    errors = extract_errors(output, "unknown")
    assert len(errors) == 5


@pytest.mark.unit
def test_no_errors_extracted():
    output = "random output with no error patterns"
    errors = extract_errors(output, "unknown")
    assert errors == []


# ---------------------------------------------------------------------------
# Introduced vs pre-existing (classify_introduced)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_in_edited_file():
    errors = [{"file": "src/app.ts", "line": "10", "message": "err"}]
    introduced, preexisting = classify_introduced(errors, {"src/app.ts"})
    assert introduced == 1
    assert preexisting == 0
    assert errors[0]["introduced"] is True


@pytest.mark.unit
def test_not_in_edited():
    errors = [{"file": "src/other.ts", "line": "5", "message": "err"}]
    introduced, preexisting = classify_introduced(errors, {"src/app.ts"})
    assert introduced == 0
    assert preexisting == 1
    assert errors[0]["introduced"] is False


@pytest.mark.unit
def test_partial_path():
    errors = [{"file": "app.ts", "line": "1", "message": "err"}]
    introduced, preexisting = classify_introduced(errors, {"/full/path/app.ts"})
    assert introduced == 1
    assert errors[0]["introduced"] is True


@pytest.mark.unit
def test_no_file_field():
    errors = [{"file": None, "line": None, "message": "err"}]
    introduced, preexisting = classify_introduced(errors, {"src/app.ts"})
    assert introduced == 0
    assert preexisting == 1
    assert errors[0]["introduced"] is False


@pytest.mark.unit
def test_no_edited_files():
    errors = [
        {"file": "src/app.ts", "line": "1", "message": "err1"},
        {"file": "src/other.ts", "line": "2", "message": "err2"},
    ]
    introduced, preexisting = classify_introduced(errors, set())
    assert introduced == 0
    assert preexisting == 2
    for err in errors:
        assert err["introduced"] is False


# ---------------------------------------------------------------------------
# Contract tests (subprocess via run_hook)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_non_bash_silent(run_hook):
    result = run_hook(
        TOOL_FAILURE_CONTEXT,
        stdin_json={"tool_name": "Edit", "tool_input": {}, "tool_response": ""},
    )
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


@pytest.mark.unit
def test_bash_errors_envelope(run_hook, make_ledger):
    make_ledger(edits=[])
    result = run_hook(
        TOOL_FAILURE_CONTEXT,
        stdin_json={
            "tool_name": "Bash",
            "tool_input": {"command": "npx tsc --noEmit"},
            "tool_response": "app.tsx(15,3): error TS2322: Type 'string' not assignable",
        },
    )
    assert result["exit_code"] == 0
    parsed = result["parsed_json"]
    assert parsed is not None
    assert "hookSpecificOutput" in parsed
    assert "additionalContext" in parsed["hookSpecificOutput"]


@pytest.mark.unit
def test_context_has_command(run_hook, make_ledger):
    make_ledger(edits=[])
    result = run_hook(
        TOOL_FAILURE_CONTEXT,
        stdin_json={
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
            "tool_response": "Error: test failed",
        },
    )
    parsed = result["parsed_json"]
    assert parsed is not None
    context = parsed["hookSpecificOutput"]["additionalContext"]
    assert "Command failed:" in context


@pytest.mark.unit
def test_context_has_type(run_hook, make_ledger):
    make_ledger(edits=[])
    result = run_hook(
        TOOL_FAILURE_CONTEXT,
        stdin_json={
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -v"},
            "tool_response": "FAILED tests/test_x.py::test_foo - AssertionError",
        },
    )
    parsed = result["parsed_json"]
    assert parsed is not None
    context = parsed["hookSpecificOutput"]["additionalContext"]
    assert "Type: test failure" in context


@pytest.mark.unit
def test_context_has_suggested(run_hook, make_ledger):
    make_ledger(edits=[{"file": "src/app.ts"}])
    result = run_hook(
        TOOL_FAILURE_CONTEXT,
        stdin_json={
            "tool_name": "Bash",
            "tool_input": {"command": "npx tsc --noEmit"},
            "tool_response": (
                "src/app.ts(10,5): error TS2322: Type mismatch\n"
                "src/other.ts(20,3): error TS2345: Arg not assignable"
            ),
        },
    )
    parsed = result["parsed_json"]
    assert parsed is not None
    context = parsed["hookSpecificOutput"]["additionalContext"]
    assert "Suggested: Fix #" in context


@pytest.mark.unit
def test_malformed_stdin_0(run_hook):
    result = run_hook(
        TOOL_FAILURE_CONTEXT,
        stdin_json="this is not valid json {{{",
    )
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""
