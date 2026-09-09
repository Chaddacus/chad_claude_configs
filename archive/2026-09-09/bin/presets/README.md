# Verify presets

Reusable acceptance gates invoked by `goose_dispatch.py --verify-preset <name>`.

Each preset is a shell script that exits 0 iff the gate passes. Callers can
compose presets with additional checks via `--acceptance-script` (a
supervisor-written script that invokes the preset and layers extra asserts).

## Contract

- Presets are executable shell scripts named `<name>.sh`.
- Arguments are passed through via `--preset-args "arg1 arg2"`.
- Exit 0 = pass. Any non-zero = fail, with a human-readable reason on stderr.
- Presets MUST NOT silently swallow failures. If a check can't run (e.g.,
  Playwright not installed), exit non-zero with a clear message — never 0.

## Available presets

### `python-strict.sh <test_pattern> <source_path>`

Runs `pytest`, `ruff`, and `mypy` (if configured). Use for Python slices
where the unit test suite is the acceptance criterion.

**Catches**: failed tests, lint violations, (optionally) type errors.
**Misses**: user-facing behavior, logic bugs outside test coverage.

### `frontend-visual.sh <url> [screenshot_out]`

Playwright-based pixel-level check. Loads the URL, asserts no page errors,
measures the canvas (or body), takes a screenshot, and asserts the rendered
output is not mostly white and has meaningful color diversity.

**Catches**: blank canvas, JS import errors, missing DOM elements.
**Misses**: layout regressions, post-interaction behavior.

### `mcp-stdio.sh <module> [required_tool1,required_tool2,...]`

Spawns an MCP server module as a subprocess, speaks the real MCP stdio
protocol, and asserts the required tools are registered.

**Catches**: import errors, tool-registration bugs, protocol non-compliance.
**Misses**: logic correctness of individual tools.

## Adding a new preset

1. Create `<name>.sh`, `chmod +x` it.
2. Document its contract at the top (args, exits, catches, misses).
3. Add an entry here.
4. Reference the new preset from `SKILL.md` if it should be a default for a
   project type.
