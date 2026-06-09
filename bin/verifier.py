"""
verifier.py — read-only snapshot + citation enforcement.

Contract
--------
This module provides two driver-owned primitives that together enforce
CR-INV-006 (no false closure) and CR-INV-009 (replan-cites-evidence):

  1. **Read-only snapshot.** `create_readonly_snapshot(source, dest_parent)`
     copies a directory tree to a fresh path and chmods every regular
     file to 0444 and every directory to 0555. The verifier subprocess
     that runs against the snapshot cannot ACCIDENTALLY mutate it: any
     write through normal file IO returns EACCES.

     `cleanup_snapshot(snapshot)` restores write permissions and removes
     the tree.

  2. **Citation enforcement.** Verifier output uses a simple textual
     contract: lines beginning with `CLAIM:` introduce a claim, and
     subsequent `CITE: <path>:<start>-<end>` lines (optionally with a
     trailing `"expected substring"`) attach evidence. The parser
     extracts the structured claim+citations list; the enforcer checks
     each citation against the snapshot:
       - cited file must exist
       - line range must be valid (1 ≤ start ≤ end ≤ file_lines)
       - if an expected substring is given, the joined content of the
         cited lines must contain it (verbatim, case-sensitive)
       - every CLAIM must have at least one CITE

     `validate_verifier_output(snapshot, text)` is the driver-facing
     entry point: it parses, enforces, AND rejects empty output. The
     driver treats `ValidationResult.ok=False` as a hard rejection.

Threat model — explicit
-----------------------
This module's snapshot defends against:
  - Accidental writes by cooperative tools (linters, formatters, the
    Python `Write`/`Edit` tools, naive verifier subprocesses).
  - Buggy verifier code that tries to "fix" what it's verifying.
  - Verifier subprocesses spawned with the same uid that respect file
    permissions (i.e., do not `chmod` first).

This module does NOT defend against:
  - A same-uid process that actively wants to bypass the boundary. Such
    a process can `chmod 0755` any snapshot directory it owns and then
    write into it. The defense for active adversaries is OS-level
    isolation (different uid, bind mount, namespace, container).
  - Symlinks in the source tree whose targets point outside the source.
    Such symlinks ARE rejected at snapshot-creation time (see below).
    A driver that wants stronger isolation should run the verifier in a
    process sandbox separately from this module.

Symlinks
--------
Outbound symlinks are rejected at snapshot creation. A symlink whose
target resolves to a path OUTSIDE the source directory would let a
verifier reading "snapshot/link" write to a file outside the snapshot.
`create_readonly_snapshot` walks the source and refuses to proceed if
any such symlink is found. Internal symlinks (target stays under the
source root) are preserved as symlinks in the snapshot.

Out of scope
------------
- Running the verifier subprocess itself.
- Network egress / time / memory limits.
- Diff-aware citation hints — citations are about post-apply file state.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


# Citation parsing format.
#
# CLAIM: <free-form claim text>
# CITE: <path>:<start>[-<end>] [ "expected substring" ]
#
# Both `CLAIM:` and `CITE:` must start at column 0. Any other line is
# ignored. Repeated CITE lines attach to the most recent CLAIM until a
# new CLAIM line.
#
# Grammar limits (encoded in the regex below):
#   - <path> may not contain `:` (it would be ambiguous with the
#     line-number separator). Verifiers should avoid colon-containing
#     paths, or the driver should sanitize them before citation.
#   - <expected substring> may not contain `"` (no escape mechanism).
#     If the verifier needs to cite a line containing a literal quote,
#     it can omit the expected substring and rely on the line range
#     alone, or split into two citations.
#   - <path> must not begin with whitespace or `:`. Leading whitespace
#     after `CITE:` is consumed.
_CLAIM_RE = re.compile(r"^CLAIM:\s*(?P<claim>.*\S.*)\s*$")
_CITE_RE = re.compile(
    r"^CITE:\s*"
    r"(?P<path>[^\s:][^:]*?)"        # path: non-empty, no leading whitespace/colon
    r":\s*"
    r"(?P<start>\d+)"
    r"(?:\s*-\s*(?P<end>\d+))?"
    r"(?:\s+\"(?P<expected>[^\"]*)\")?"
    r"\s*$"
)


@dataclass
class Citation:
    path: str
    line_start: int
    line_end: int  # inclusive; equals line_start for single-line citations
    expected_substring: Optional[str] = None
    raw: str = ""  # original line for diagnostics


@dataclass
class ClaimEvidence:
    claim: str
    citations: List[Citation] = field(default_factory=list)


@dataclass
class CitationError:
    citation: Citation
    reason: str  # "file_not_found" | "range_invalid" | "substring_not_found"
    detail: str = ""


@dataclass
class ValidationResult:
    """Driver-facing combined result from parse + enforce + claim-count check."""
    ok: bool
    error: Optional[str] = None
    claims: List["ClaimEvidence"] = field(default_factory=list)
    citation_errors: List["CitationError"] = field(default_factory=list)
    claims_without_citations: List["ClaimEvidence"] = field(default_factory=list)


@dataclass
class SnapshotResult:
    ok: bool
    path: Optional[str]
    error: Optional[str] = None


# -- Snapshot ----------------------------------------------------------


def _check_no_outbound_symlinks(source: Path) -> Optional[str]:
    """Walk source and return an error string if ANY symlink is unsafe
    for `copytree(..., symlinks=True)` snapshot semantics.

    Two classes of unsafe symlinks:

    1. **Absolute symlinks.** A symlink whose RAW LINK TEXT is absolute
       (e.g., `link -> /full/path/to/a.py`) keeps that absolute path
       after copytree. Even if `/full/path/to/a.py` is inside the
       source root, the COPIED symlink in the snapshot still points to
       the ORIGINAL source file, not the snapshot's copy. A verifier
       writing through `snapshot/link` would mutate the original source
       — invisible to the read-only chmod and outside the snapshot
       boundary.

       Fix: reject ALL absolute symlinks. The driver should rewrite
       them to relative paths before invoking snapshot, or accept the
       reject.

    2. **Outbound relative symlinks.** A relative symlink whose
       resolved target falls outside source (e.g., `link -> ../outside`)
       would let a verifier write to an external file too.

    Returns None on clean tree.
    """
    try:
        source_resolved = source.resolve()
    except OSError as exc:
        return f"failed to resolve source: {exc}"
    for root, dirs, files in os.walk(source, followlinks=False):
        root_path = Path(root)
        for name in list(dirs) + list(files):
            p = root_path / name
            if not p.is_symlink():
                continue
            # Read raw link text (does NOT resolve).
            try:
                raw_target = os.readlink(p)
            except OSError as exc:
                return f"symlink {p} readlink failed: {exc}"
            if os.path.isabs(raw_target):
                return (
                    f"absolute symlink {p} -> {raw_target!r}: after copytree "
                    f"the link in the snapshot would still target the original "
                    f"path, allowing writes outside the snapshot"
                )
            # Relative target. Resolve via the symlink's directory.
            try:
                target = p.resolve()
            except (OSError, RuntimeError) as exc:
                # RuntimeError covers symlink-loop detection from
                # Path.resolve() (e.g., `link -> link`). OSError covers
                # filesystem errors. Either way, fail closed.
                return f"symlink {p} resolution failed: {exc}"
            try:
                target.relative_to(source_resolved)
            except ValueError:
                return f"outbound symlink {p} -> {target} escapes source"
    return None


def create_readonly_snapshot(
    source: Path,
    dest_parent: Optional[Path] = None,
) -> SnapshotResult:
    """Copy `source` to a fresh path and chmod everything read-only.

    Returns SnapshotResult with the snapshot path on success or an
    error message on failure. The snapshot lives under `dest_parent`
    (default: tempdir/verifier-snap) with a uuid suffix.

    Refuses to snapshot a tree containing unsafe symlinks. Two reject
    classes (see `_check_no_outbound_symlinks` for full rationale):

      - ABSOLUTE symlinks (any). After copytree, an absolute target
        still points to the original location, NOT the snapshot copy.
        Writes through `snapshot/abs_link` would mutate outside the
        snapshot. All absolute symlinks are rejected — regardless of
        whether the target happens to be inside the source.
      - RELATIVE OUTBOUND symlinks. A relative target that resolves
        outside the source root would escape similarly.

    Only RELATIVE INBOUND symlinks are preserved. Their text remains
    relative, so the same relative resolution works in the snapshot.

    Symlink-loop detection (e.g., `link -> link`) is caught as a
    snapshot-creation failure (resolution raises RuntimeError).

    chmod policy:
      - regular files: 0444
      - directories: 0555
      - internal relative symlinks: untouched (the link node itself —
        the chmod walk skips symlinks)

    If ANY chmod fails on a file or directory we own in the snapshot,
    we treat the snapshot as compromised: clean it up and return
    ok=False. The contract is "every regular file is 0444"; partial
    application breaks that.
    """
    source = Path(source)
    if not source.exists():
        return SnapshotResult(ok=False, path=None, error=f"source not found: {source}")
    if not source.is_dir():
        return SnapshotResult(ok=False, path=None, error=f"source is not a directory: {source}")

    outbound = _check_no_outbound_symlinks(source)
    if outbound is not None:
        return SnapshotResult(ok=False, path=None, error=outbound)

    parent = Path(dest_parent) if dest_parent else Path(tempfile.gettempdir()) / "verifier-snap"
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return SnapshotResult(ok=False, path=None, error=f"failed to create parent: {exc}")

    snapshot = parent / f"snap-{uuid.uuid4().hex[:12]}"
    try:
        shutil.copytree(source, snapshot, symlinks=True)
    except OSError as exc:
        shutil.rmtree(snapshot, ignore_errors=True)
        return SnapshotResult(ok=False, path=None, error=f"copytree failed: {exc}")

    chmod_failures: List[str] = []
    try:
        for root, dirs, files in os.walk(snapshot, followlinks=False):
            root_path = Path(root)
            for name in files:
                p = root_path / name
                if p.is_symlink():
                    continue
                try:
                    os.chmod(p, 0o444)
                except OSError as exc:
                    chmod_failures.append(f"{p}: {exc}")
            for name in dirs:
                p = root_path / name
                if p.is_symlink():
                    continue
                try:
                    os.chmod(p, 0o555)
                except OSError as exc:
                    chmod_failures.append(f"{p}: {exc}")
        try:
            os.chmod(snapshot, 0o555)
        except OSError as exc:
            chmod_failures.append(f"{snapshot}: {exc}")
    except OSError as exc:
        _restore_writable(snapshot)
        shutil.rmtree(snapshot, ignore_errors=True)
        return SnapshotResult(ok=False, path=None, error=f"chmod walk failed: {exc}")

    if chmod_failures:
        _restore_writable(snapshot)
        shutil.rmtree(snapshot, ignore_errors=True)
        return SnapshotResult(
            ok=False,
            path=None,
            error=f"{len(chmod_failures)} chmod failure(s); first: {chmod_failures[0]}",
        )

    return SnapshotResult(ok=True, path=str(snapshot))


def _restore_writable(snapshot: Path) -> None:
    """Restore user write permission on the snapshot so it can be
    removed by rmtree. Walks bottom-up so dirs can be written into."""
    if not snapshot.exists():
        return
    for root, dirs, files in os.walk(snapshot, topdown=False, followlinks=False):
        root_path = Path(root)
        for name in files:
            p = root_path / name
            if p.is_symlink():
                continue
            try:
                os.chmod(p, 0o644)
            except OSError:
                pass
        for name in dirs:
            p = root_path / name
            if p.is_symlink():
                continue
            try:
                os.chmod(p, 0o755)
            except OSError:
                pass
    try:
        os.chmod(snapshot, 0o755)
    except OSError:
        pass


def cleanup_snapshot(snapshot: Path) -> bool:
    """Restore writable perms and remove the snapshot. Returns True on
    full cleanup, False on partial."""
    snapshot = Path(snapshot)
    if not snapshot.exists():
        return True
    _restore_writable(snapshot)
    errors: List[Exception] = []

    def _onerror(func, path, excinfo):
        errors.append(excinfo[1])

    shutil.rmtree(snapshot, onerror=_onerror)
    return not errors and not snapshot.exists()


# -- Citation parsing -------------------------------------------------


def parse_verifier_output(output: str) -> List[ClaimEvidence]:
    """Parse CLAIM/CITE lines from verifier output.

    Returns a list of `ClaimEvidence`. Lines that are neither CLAIM nor
    CITE are ignored. CITE lines without a preceding CLAIM are silently
    dropped (they don't belong to anything).
    """
    out: List[ClaimEvidence] = []
    current: Optional[ClaimEvidence] = None
    for raw_line in output.splitlines():
        m_claim = _CLAIM_RE.match(raw_line)
        if m_claim:
            current = ClaimEvidence(claim=m_claim.group("claim").strip(), citations=[])
            out.append(current)
            continue
        m_cite = _CITE_RE.match(raw_line)
        if m_cite and current is not None:
            try:
                start = int(m_cite.group("start"))
            except ValueError:
                continue
            end_str = m_cite.group("end")
            try:
                end = int(end_str) if end_str is not None else start
            except ValueError:
                continue
            current.citations.append(
                Citation(
                    path=m_cite.group("path").strip(),
                    line_start=start,
                    line_end=end,
                    expected_substring=m_cite.group("expected"),
                    raw=raw_line,
                )
            )
    return out


# -- Citation enforcement ---------------------------------------------


def enforce_citations(
    snapshot: Path,
    claims: List[ClaimEvidence],
) -> Tuple[List[CitationError], List[ClaimEvidence]]:
    """Validate each citation against the snapshot.

    Returns (errors, claims_without_citations).

    - `errors` is a flat list of CitationError, one per failed citation.
    - `claims_without_citations` is the subset of claims that have an
      empty citation list — they violate CR-INV-009.

    A citation passes when:
      - the file (snapshot / citation.path) exists and is a regular file
      - 1 ≤ line_start ≤ line_end ≤ total_lines_in_file
      - if expected_substring is given, the joined content of
        lines[line_start-1 : line_end] contains it as a substring
        (case-sensitive)
    """
    snapshot = Path(snapshot)
    errors: List[CitationError] = []
    claims_no_cite: List[ClaimEvidence] = []

    # Cache file content so multiple citations against the same file
    # only read it once.
    cache: dict = {}

    def _load(rel: str) -> Optional[List[str]]:
        if rel in cache:
            return cache[rel]
        # Reject path traversal: any component of `..` or absolute path.
        if rel.startswith("/"):
            cache[rel] = None
            return None
        norm = Path(rel)
        # Use parts() so Windows-style backslashes don't slip a `..`.
        if ".." in norm.parts:
            cache[rel] = None
            return None
        target = (snapshot / norm).resolve()
        # Ensure the resolved target stays under snapshot. resolve()
        # follows symlinks; if a symlink points outside snapshot, reject.
        try:
            snap_resolved = snapshot.resolve()
        except OSError:
            cache[rel] = None
            return None
        try:
            target.relative_to(snap_resolved)
        except ValueError:
            cache[rel] = None
            return None
        if not target.exists() or not target.is_file():
            cache[rel] = None
            return None
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            cache[rel] = None
            return None
        # `splitlines()` does NOT emit a trailing empty entry for a file
        # ending in `\n`, so the line count matches the real file. A
        # 3-line file with terminal newline has 3 entries, not 4 (which
        # `split('\n')` would produce — letting a phantom CITE:...:4
        # pass enforcement).
        cache[rel] = text.splitlines()
        return cache[rel]

    for claim in claims:
        if not claim.citations:
            claims_no_cite.append(claim)
            continue
        for cite in claim.citations:
            lines = _load(cite.path)
            if lines is None:
                errors.append(
                    CitationError(
                        citation=cite,
                        reason="file_not_found",
                        detail=f"{cite.path!r} not present in snapshot",
                    )
                )
                continue
            n = len(lines)
            if cite.line_start < 1 or cite.line_end < cite.line_start or cite.line_end > n:
                errors.append(
                    CitationError(
                        citation=cite,
                        reason="range_invalid",
                        detail=(
                            f"range {cite.line_start}-{cite.line_end} invalid; "
                            f"file has {n} lines"
                        ),
                    )
                )
                continue
            if cite.expected_substring is not None:
                window = "\n".join(lines[cite.line_start - 1 : cite.line_end])
                if cite.expected_substring not in window:
                    errors.append(
                        CitationError(
                            citation=cite,
                            reason="substring_not_found",
                            detail=(
                                f"expected substring {cite.expected_substring!r} "
                                f"not in lines {cite.line_start}-{cite.line_end}"
                            ),
                        )
                    )

    return errors, claims_no_cite


def validate_verifier_output(
    snapshot: Path,
    output: str,
) -> ValidationResult:
    """Driver-facing entry point.

    Combines parse_verifier_output + enforce_citations and enforces the
    additional driver-level invariant that an empty CLAIM list is a
    failure. An "empty" verifier output (no `CLAIM:` lines at all) is
    treated as a closure attempt without evidence — the same failure
    class as a CLAIM with no CITE.

    Returns ValidationResult with ok=True only when:
      - at least one CLAIM was parsed
      - every CLAIM has at least one CITE
      - every CITE passes enforcement against the snapshot
    """
    claims = parse_verifier_output(output)
    if not claims:
        return ValidationResult(
            ok=False,
            error="verifier output contains no CLAIM lines",
            claims=[],
        )

    citation_errors, claims_no_cite = enforce_citations(snapshot, claims)
    if claims_no_cite:
        return ValidationResult(
            ok=False,
            error=f"{len(claims_no_cite)} claim(s) without citations",
            claims=claims,
            claims_without_citations=claims_no_cite,
            citation_errors=citation_errors,
        )
    if citation_errors:
        return ValidationResult(
            ok=False,
            error=f"{len(citation_errors)} citation error(s)",
            claims=claims,
            citation_errors=citation_errors,
            claims_without_citations=[],
        )
    return ValidationResult(
        ok=True,
        claims=claims,
    )
