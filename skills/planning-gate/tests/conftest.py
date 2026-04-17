"""Shared fixtures for planning-gate tests."""

from __future__ import annotations

import shutil
import sys

import pytest

_MIN_VERSION = (3, 11)


def _find_python_311() -> str:
    """Return a Python >= 3.11 interpreter path, or fall back to sys.executable."""
    if sys.version_info >= _MIN_VERSION:
        return sys.executable
    for name in ("python3.13", "python3.12", "python3.11"):
        path = shutil.which(name)
        if path:
            return path
    return sys.executable


PYTHON_311: str = _find_python_311()

needs_python_311 = pytest.mark.skipif(
    sys.version_info < _MIN_VERSION and PYTHON_311 == sys.executable,
    reason=f"requires Python >= {'.'.join(map(str, _MIN_VERSION))} (found {sys.version})",
)
