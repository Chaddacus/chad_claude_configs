#!/bin/bash
# python-strict: pytest + ruff (+ mypy if configured).
#
# Usage:
#   python-strict.sh <test_pattern> <source_path>
# Example:
#   python-strict.sh tests/test_foo.py backend/foo.py
#
# Contract:
#   - Exits 0 iff all checks pass.
#   - Runs pytest -q on <test_pattern>.
#   - Runs ruff check on <source_path>.
#   - Runs mypy on <source_path> IFF the project has [tool.mypy] in pyproject.toml
#     or a py.typed marker.
# What this catches:
#   - Failed tests, flake8-class lint issues, (optionally) type errors.
# What this does NOT catch:
#   - Actual user-facing behavior (see frontend-visual.sh for that).
#   - Logic bugs that are not covered by the provided tests.

set -u
if [ $# -lt 2 ]; then
  echo "usage: python-strict.sh <test_pattern> <source_path>" >&2
  exit 2
fi
tests="$1"
src="$2"

echo ">> pytest $tests"
uv run pytest "$tests" -q || exit 1

echo ">> ruff check $src"
uv run ruff check "$src" || exit 1

if grep -q "^\[tool.mypy\]" pyproject.toml 2>/dev/null || [ -f "$(dirname "$src")/py.typed" ]; then
  echo ">> mypy $src"
  uv run mypy "$src" || exit 1
else
  echo ">> mypy: project not configured for mypy; skipping"
fi

echo "OK python-strict passed"
