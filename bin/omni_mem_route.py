#!/usr/bin/env python3
"""cwd -> omni-mem container selector for the work/personal memory vault split.

Chad's memory is split across two omni-mem containers (2026-07-13):
  - omni-mem          (port 8765) — the WORK vault, and the default everywhere
  - omni-mem-personal (port 8767) — the PERSONAL vault, own DB file/volume

Anything running with a cwd inside ~/chad_personal routes to the personal
vault; everything else routes to the main one. Import `container_for_cwd`
from Python hooks, or invoke this file directly from shell:

    CONTAINER="$(python3 ~/.claude/bin/omni_mem_route.py)"

An explicit OMNI_MEM_CONTAINER env var overrides the cwd rule in both cases.
"""

from __future__ import annotations

import os
from pathlib import Path

PERSONAL_TREE = Path.home() / "chad_personal"
DEFAULT_CONTAINER = "omni-mem"
PERSONAL_CONTAINER = "omni-mem-personal"


def container_for_cwd(cwd: str | None = None) -> str:
    """Return the omni-mem container name for this working directory.

    Precedence: explicit OMNI_MEM_CONTAINER env override; else the personal
    container when cwd is inside ~/chad_personal; else the main (work) one.
    Falls back to CLAUDE_PROJECT_DIR, then os.getcwd(), when cwd is not given.
    """
    override = os.environ.get("OMNI_MEM_CONTAINER")
    if override:
        return override
    raw = cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        path = Path(raw).resolve()
    except OSError:
        return DEFAULT_CONTAINER
    return PERSONAL_CONTAINER if path.is_relative_to(PERSONAL_TREE) else DEFAULT_CONTAINER


if __name__ == "__main__":
    print(container_for_cwd())
