#!/usr/bin/env bash
# Detect the test runner for the repo at $1 (or $PWD).
# Prints one of: pytest | jest | unsupported
# Exits 0 always — caller decides what to do with "unsupported".

set -euo pipefail

REPO="${1:-$PWD}"
cd "$REPO" 2>/dev/null || { echo "unsupported"; exit 0; }

# pytest: pyproject.toml with pytest section, OR pytest.ini, OR tox.ini with pytest, OR setup.cfg with [tool:pytest]
if [[ -f pyproject.toml ]] && grep -qE '^\[tool\.pytest' pyproject.toml 2>/dev/null; then
    echo "pytest"; exit 0
fi
if [[ -f pytest.ini ]] || [[ -f conftest.py ]]; then
    echo "pytest"; exit 0
fi
if [[ -f setup.cfg ]] && grep -qE '^\[tool:pytest\]' setup.cfg 2>/dev/null; then
    echo "pytest"; exit 0
fi

# jest/vitest: package.json declaring jest or vitest
if [[ -f package.json ]]; then
    if grep -qE '"jest"\s*:' package.json 2>/dev/null \
       || grep -qE '"vitest"\s*:' package.json 2>/dev/null \
       || [[ -f jest.config.js ]] || [[ -f jest.config.ts ]] \
       || [[ -f vitest.config.js ]] || [[ -f vitest.config.ts ]]; then
        echo "jest"; exit 0
    fi
fi

# Fallback: only assume pytest if there's an actual python project signal (pyproject.toml or setup.py)
# AND a tests/ directory or top-level test_*.py — avoids false positives in random dirs (/tmp, etc).
if [[ -f pyproject.toml ]] || [[ -f setup.py ]]; then
    if [[ -d tests ]] || compgen -G "test_*.py" >/dev/null || compgen -G "*_test.py" >/dev/null; then
        echo "pytest"; exit 0
    fi
fi

echo "unsupported"
