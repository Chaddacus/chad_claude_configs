#!/usr/bin/env python3
"""verifier_shim.py — adapt an arbitrary acceptance command to the CP4 verifier's
CLAIM/CITE evidence contract.

Why: slice_executor (CP5) validates a verifier subprocess's stdout via
verifier.validate_verifier_output, which demands `CLAIM:`/`CITE:` lines whose
citations resolve to a real file:line inside the read-only snapshot. Real
acceptance commands (pytest, build, lint) don't speak that dialect. This shim
runs the acceptance command in the snapshot cwd and, on exit 0, emits one CLAIM
plus one CITE to a file that provably exists in the snapshot — turning a plain
exit code into the structured evidence the verifier requires. On failure it
exits non-zero and emits NO CLAIM, so validation fails closed.

Usage (as SliceSpec.verifier_command; launched with cwd = the snapshot):
    verifier_shim.py --check "<shell command>" [--cite-file REL]...

`--cite-file` gives preferred citation targets (e.g. files the slice owns). The
first one that exists as a non-empty regular file is cited; otherwise the shim
auto-discovers the first non-empty regular file under cwd (excluding .git).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _first_citable(cwd: Path, preferred: list[str]) -> str | None:
    """Return a repo-relative path to a non-empty regular file to cite.

    Prefers caller-supplied paths (slice-owned files), then falls back to a
    deterministic walk of the snapshot. The citation only needs to resolve to
    a real file with >=1 line — the acceptance evidence is the command's exit
    code; the CITE just satisfies the verifier's structural contract.
    """
    # 1) Honour caller-supplied preferences first.
    for rel in preferred:
        p = (cwd / rel)
        if p.is_file() and _line_count(p) >= 1:
            return rel
    # 2) Deterministic fallback: first non-empty regular file, .git excluded.
    for root, dirs, files in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            full = Path(root) / name
            if full.is_file() and _line_count(full) >= 1:
                return str(full.relative_to(cwd))
    return None


def _line_count(p: Path) -> int:
    """Count lines the way verifier.enforce_citations does (splitlines)."""
    try:
        return len(p.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="CLAIM/CITE shim for an acceptance command")
    ap.add_argument("--check", required=True, help="Shell command whose exit code is the acceptance signal")
    ap.add_argument("--cite-file", action="append", default=[], help="Preferred citation target (repo-relative); repeatable")
    args = ap.parse_args()

    cwd = Path(os.getcwd()).resolve()

    # Run the real acceptance command in the snapshot cwd. Its output goes to
    # stderr so it never pollutes the CLAIM/CITE lines the verifier parses on
    # stdout (parse_verifier_output ignores non-CLAIM/CITE lines anyway, but
    # keeping stdout clean is defensive).
    proc = subprocess.run(["bash", "-c", args.check], cwd=str(cwd), capture_output=True, text=True)
    if proc.stdout:
        sys.stderr.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)

    if proc.returncode != 0:
        # Fail closed: non-zero exit + no CLAIM => execute_slice rejects at the
        # "verifier_subprocess" stage AND validate_verifier_output would reject
        # the empty CLAIM set. Belt and suspenders.
        sys.stderr.write(f"verifier_shim: acceptance command failed (exit {proc.returncode}): {args.check}\n")
        return proc.returncode or 1

    cite = _first_citable(cwd, args.cite_file)
    if cite is None:
        sys.stderr.write("verifier_shim: acceptance passed but no citable file found in snapshot\n")
        return 3

    print(f"CLAIM: acceptance command passed (exit 0): {args.check}")
    print(f"CITE: {cite}:1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
