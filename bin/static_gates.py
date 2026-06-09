"""
static_gates.py — driver-owned static gates for worker diffs.

Contract
--------
Given a unified diff (bytes), a main repo path, and a base SHA the diff was
generated against, this module runs PRE-MERGE static checks. The gate
decides whether a diff is safe to even rehearse, before any state-mutating
work (CP2 apply_rehearsal). If the gate rejects, the diff never touches main.

The gates are intentionally minimal and driver-owned (not pluggable, not
worker-overridable). They exist to enforce CR-INV-011 (simple-is-better)
and basic code-safety boundaries on AI-generated worker output.

Gates
-----
1. **ast.parse**: every .py file the diff touches (post-apply content) must
   parse as valid Python. Syntactically broken code never makes it past
   the gate.

2. **Banned constructs (AST visitor)**: for .py files, the gate detects
   the following structural patterns:
   - `Name(id ∈ {eval, exec, compile, __import__}, ctx=Load)` — any
     reference to these builtins (catches `fn = eval; fn(x)`, not just
     literal `eval(`).
   - `Call(keywords=[..., keyword(arg='shell', value=Constant(True)), ...])`
     — any function call with `shell=True`.
   - `ExceptHandler(type=None)` — bare `except:`.
   - `ImportFrom` with `alias(name='*')` — wildcard import.

   For ADD-shaped changes (new file, rename from non-.py to .py), every
   line in the post-apply file is in scope. For MODIFY/RENAME-within-.py
   changes, only lines that the diff ADDED are in scope (computed via
   per-file `git diff -U0 [-M] HEAD`).

Out of scope
------------
- Type checks (mypy/pyright), style (black/ruff), test coverage.
- Project-specific patterns; CP3 enforces only universal anti-patterns.
- Languages other than Python.
- Dynamic dispatch through `getattr` / strings (e.g.,
  `getattr(__builtins__, "eval")(...)`). A driver-owned gate that
  rejected those would have unacceptable false-positive rates. Document
  this as an explicit limitation.

Mechanics
---------
The gate sets up a disposable sibling worktree at base_sha (just like
CP2), applies the diff, asks `git diff --name-status -z HEAD` for the
authoritative changed-path bytes, runs ast.parse + AST inspection, then
cleans up. Main is never touched.

Authoritative paths from git's NUL-separated output are used because
unified-diff headers C-quote non-ASCII / whitespace-containing paths.
A hand-parsed `--- a/X` header on `"unicod\303\251.py"` would fail to
match `path.endswith(".py")` and silently skip the file.

The sibling worktree shares main's .git, so we use the per-call private
hooks dir + `_safe_git` discipline imported from apply_rehearsal.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from apply_rehearsal import (
    SandboxError,
    _make_private_hooks_dir,
    _safe_git,
)


# Banned Name references (Load context only — does not catch
# definitions like `def eval(): pass`). Maps Python name → (pattern_name,
# rationale). Pattern names match the legacy regex names for callers
# that switch-on them.
_BANNED_NAMES: Dict[str, Tuple[str, str]] = {
    "eval": ("eval_call", "eval() executes arbitrary expressions — code-injection vector"),
    "exec": ("exec_call", "exec() executes arbitrary statements — code-injection vector"),
    "compile": ("compile_call", "compile() builds code objects from strings — code-injection vector"),
    "__import__": ("dunder_import", "__import__() bypasses static import analysis"),
}

# NOTE: A regex pass was tried in CP3 R1 alongside the AST visitor. It
# was dropped because (a) the AST is comprehensive for the patterns we
# care about and (b) regex on raw source generates false positives like
# `def eval(self):` matching `eval(`. The AST visitor below is the
# single authoritative check.


@dataclass
class ParseError:
    path: str
    message: str
    lineno: Optional[int]
    offset: Optional[int]


@dataclass
class BannedFinding:
    path: str
    lineno: int  # line number in POST-apply file
    pattern_name: str
    rationale: str
    source: str  # always "ast" — regex layer was dropped in R2


@dataclass
class GateResult:
    ok: bool
    stage: str
    error: Optional[str] = None
    parse_errors: List[ParseError] = field(default_factory=list)
    banned_findings: List[BannedFinding] = field(default_factory=list)
    files_checked: List[str] = field(default_factory=list)
    diff_bytes_len: int = 0
    base_sha: str = ""
    sibling_path: Optional[str] = None
    cleanup_failed: bool = False
    cleanup_output: str = ""


@dataclass
class _ChangedFile:
    """Post-apply description of a changed file from `git diff --name-status -z`."""
    status: str   # "A", "M", "D", "R", "C", "T", "U" (Git status letters)
    path: bytes   # post-apply path bytes
    old_path: Optional[bytes] = None  # for R/C, the pre-rename path


def _name_status_z(repo: Path) -> List[_ChangedFile]:
    """Run `git diff --name-status -z HEAD` in `repo` and parse.

    Raises SandboxError on failure (fail-closed).
    """
    proc = subprocess.run(
        ["git", "diff", "--name-status", "-z", "HEAD"],
        cwd=str(repo),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise SandboxError(
            f"git diff --name-status -z failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    raw = proc.stdout
    if not raw:
        return []
    parts = raw.split(b"\x00")
    if parts and parts[-1] == b"":
        parts.pop()

    out: List[_ChangedFile] = []
    i = 0
    while i < len(parts):
        status_field = parts[i]
        if not status_field:
            i += 1
            continue
        # Status field may be a single letter (e.g., b"M") or letter+score
        # (e.g., b"R100", b"C075").
        letter = chr(status_field[0])
        if letter in ("R", "C"):
            # Two paths follow.
            old_path = parts[i + 1] if i + 1 < len(parts) else b""
            new_path = parts[i + 2] if i + 2 < len(parts) else b""
            out.append(_ChangedFile(status=letter, path=new_path, old_path=old_path))
            i += 3
        else:
            path = parts[i + 1] if i + 1 < len(parts) else b""
            out.append(_ChangedFile(status=letter, path=path))
            i += 2
    return out


def _added_line_set_for_file(
    repo: Path,
    new_path: bytes,
    old_path: Optional[bytes] = None,
) -> Set[int]:
    """Return the set of 1-indexed line numbers ADDED in the post-apply
    file for the given path.

    For a rename within Python (`old_path != new_path`), uses `-M` and
    passes BOTH paths so git pairs them and reports the actual content
    delta rather than treating new_path as fully added.

    Parses `@@ ... +A,B @@` headers and enumerates added (`+`-prefixed)
    line numbers. CRITICAL: once inside a hunk, only the FIRST character
    of each line determines its type. Content lines like `+++x` (a
    Python prefix-increment-like expression) are added lines whose
    content happens to start with `++`. They are NOT header lines.

    File headers (`+++ b/path` / `--- a/path`) only appear BEFORE the
    first `@@` of a file's section. Since this helper requests a
    per-file diff, there is exactly one file section.

    Raises SandboxError on failure (fail-closed).
    """
    if old_path is not None and old_path != new_path:
        cmd = [
            "git", "diff", "-U0", "-M", "HEAD", "--",
            os.fsdecode(old_path), os.fsdecode(new_path),
        ]
    else:
        cmd = ["git", "diff", "-U0", "HEAD", "--", os.fsdecode(new_path)]
    proc = subprocess.run(cmd, cwd=str(repo), capture_output=True)
    if proc.returncode != 0:
        raise SandboxError(
            f"git diff -U0 for {new_path!r} failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    out: Set[int] = set()
    text = proc.stdout.decode("utf-8", errors="replace")
    hunk_re = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@")
    cur_line = 0
    in_hunk = False
    for line in text.split("\n"):
        m = hunk_re.match(line)
        if m:
            cur_line = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            in_hunk = count > 0
            continue
        if not in_hunk:
            # Header lines (`+++ b/path`, `--- a/path`, `diff --git ...`)
            # live here. We don't need to classify them; we just don't
            # treat them as added content.
            continue
        # Inside a hunk. Classify by the FIRST CHARACTER only.
        if not line:
            # Empty added line (rare in unified diff but possible).
            continue
        first = line[0]
        if first == "+":
            out.add(cur_line)
            cur_line += 1
        elif first == "-":
            # Deletion does not advance post-apply lineno.
            continue
        elif first == "\\":
            # `\ No newline at end of file` marker.
            continue
        else:
            # Context line (space prefix in unified diff). -U0 should
            # not emit these, but be defensive.
            cur_line += 1
    return out


def _walk_ast_banned(tree: ast.AST, source_lines: List[str]) -> List[Tuple[int, str, str, str]]:
    """Walk an AST and emit banned findings.

    Returns list of (lineno, pattern_name, rationale, source="ast").
    """
    out: List[Tuple[int, str, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in _BANNED_NAMES:
                pat_name, rationale = _BANNED_NAMES[node.id]
                out.append((
                    node.lineno,
                    pat_name,
                    rationale,
                    "ast",
                ))
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    out.append((
                        node.lineno,
                        "shell_true",
                        "subprocess shell=True enables shell injection",
                        "ast",
                    ))
        elif isinstance(node, ast.ExceptHandler) and node.type is None:
            out.append((
                node.lineno,
                "bare_except",
                "bare except swallows KeyboardInterrupt and masks bugs",
                "ast",
            ))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    out.append((
                        node.lineno,
                        "wildcard_import",
                        "wildcard import pollutes namespace",
                        "ast",
                    ))
    return out


def static_gate(
    *,
    main_repo: Path,
    base_sha: str,
    diff_bytes: bytes,
    rehearsal_parent: Optional[Path] = None,
) -> GateResult:
    """Run driver-owned static gates against a diff.

    See module docstring for the contract. Main repo is never mutated.
    """
    main_repo = Path(main_repo)
    diff_len = len(diff_bytes)

    if diff_len == 0:
        return GateResult(
            ok=False,
            stage="empty",
            error="diff is empty bytes",
            base_sha=base_sha,
            diff_bytes_len=0,
        )

    try:
        hooks_dir = _make_private_hooks_dir()
    except OSError as exc:
        return GateResult(
            ok=False,
            stage="setup",
            error=f"failed to create private hooks dir: {exc}",
            base_sha=base_sha,
            diff_bytes_len=diff_len,
        )

    parent = (
        Path(rehearsal_parent)
        if rehearsal_parent
        else Path(tempfile.gettempdir()) / "static-gate"
    )
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        shutil.rmtree(hooks_dir, ignore_errors=True)
        return GateResult(
            ok=False,
            stage="setup",
            error=f"failed to create gate parent dir: {exc}",
            base_sha=base_sha,
            diff_bytes_len=diff_len,
        )

    sibling = parent / f"gate-{uuid.uuid4().hex[:12]}"
    sibling_created = False
    result = GateResult(
        ok=False,
        stage="setup",
        base_sha=base_sha,
        diff_bytes_len=diff_len,
        sibling_path=str(sibling),
    )

    try:
        add_proc = _safe_git(
            main_repo,
            ["worktree", "add", "--detach", str(sibling), base_sha],
            hooks_dir=hooks_dir,
        )
        if add_proc.returncode != 0:
            result.stage = "setup"
            result.error = f"sibling worktree creation failed: {add_proc.stderr.strip()}"
            return result
        sibling_created = True

        # Apply diff in sibling. We use --index because that's what gives
        # us a clean `git diff --name-status -z HEAD` result. The post-
        # apply files are still on disk for ast.parse.
        apply_proc = _safe_git(
            sibling,
            ["apply", "--index", "--binary", "-"],
            hooks_dir=hooks_dir,
            input=diff_bytes,
            text=False,
        )
        if apply_proc.returncode != 0:
            stderr = (apply_proc.stderr or b"").decode("utf-8", errors="replace").strip()
            result.stage = "apply"
            result.error = f"diff failed to apply in gate worktree: {stderr}"
            return result

        # Authoritative list of changed paths via git's NUL-separated output.
        try:
            changes = _name_status_z(sibling)
        except SandboxError as exc:
            result.stage = "apply"
            result.error = f"failed to enumerate changes: {exc}"
            return result

        # Filter to Python files we should inspect. Deletions are skipped
        # because the file doesn't exist post-apply.
        py_changes: List[_ChangedFile] = []
        for ch in changes:
            if ch.status == "D":
                continue
            if ch.path.endswith(b".py"):
                py_changes.append(ch)

        result.files_checked = [
            os.fsdecode(ch.path) for ch in py_changes
        ]

        # Parse each.
        parsed_trees: List[Tuple[_ChangedFile, Optional[ast.AST], List[str]]] = []
        for ch in py_changes:
            full_bytes = os.fsencode(str(sibling)) + b"/" + ch.path
            path_disp = os.fsdecode(ch.path)
            try:
                with open(full_bytes, "rb") as fh:
                    raw = fh.read()
            except OSError as exc:
                result.parse_errors.append(
                    ParseError(path=path_disp, message=f"read failed: {exc}", lineno=None, offset=None)
                )
                parsed_trees.append((ch, None, []))
                continue
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                result.parse_errors.append(
                    ParseError(
                        path=path_disp,
                        message=f"not valid UTF-8: {exc}",
                        lineno=None,
                        offset=None,
                    )
                )
                parsed_trees.append((ch, None, []))
                continue
            try:
                tree = ast.parse(content, filename=path_disp)
            except SyntaxError as exc:
                result.parse_errors.append(
                    ParseError(
                        path=path_disp,
                        message=str(exc.msg or "syntax error"),
                        lineno=exc.lineno,
                        offset=exc.offset,
                    )
                )
                parsed_trees.append((ch, None, content.split("\n")))
                continue
            parsed_trees.append((ch, tree, content.split("\n")))

        # Banned-construct inspection.
        for ch, tree, source_lines in parsed_trees:
            path_disp = os.fsdecode(ch.path)
            # Compute the "in-scope lines" for THIS file.
            # - status "A": brand-new file → all lines in scope.
            # - status "R" or "C": only flag as full-scope if the OLD
            #   path was non-.py (so the file effectively "becomes"
            #   Python here). Otherwise treat as modification.
            # - status "M" or "T" (typechange) or "R"/"C" within .py:
            #   use added-line set from `git diff -U0`.
            in_scope_lines: Optional[Set[int]] = None  # None = all lines
            if ch.status == "A":
                in_scope_lines = None
            elif ch.status in ("R", "C"):
                old_is_py = ch.old_path is not None and ch.old_path.endswith(b".py")
                if not old_is_py:
                    in_scope_lines = None  # Becoming Python — whole file in scope.
                else:
                    # Rename within .py: pass both paths so git pairs
                    # them with -M and we get the real content delta,
                    # not a full-file "added" diff.
                    try:
                        in_scope_lines = _added_line_set_for_file(
                            sibling, ch.path, old_path=ch.old_path,
                        )
                    except SandboxError as exc:
                        result.stage = "apply"
                        result.error = f"failed to compute added lines: {exc}"
                        return result
            else:
                try:
                    in_scope_lines = _added_line_set_for_file(sibling, ch.path)
                except SandboxError as exc:
                    result.stage = "apply"
                    result.error = f"failed to compute added lines: {exc}"
                    return result

            # AST-based findings.
            if tree is not None:
                for lineno, name, rationale, src in _walk_ast_banned(tree, source_lines):
                    if in_scope_lines is None or lineno in in_scope_lines:
                        result.banned_findings.append(
                            BannedFinding(
                                path=path_disp,
                                lineno=lineno,
                                pattern_name=name,
                                rationale=rationale,
                                source=src,
                            )
                        )

        if result.parse_errors:
            result.stage = "parse"
            result.error = (
                f"{len(result.parse_errors)} parse error(s) in changed Python files"
            )
            return result
        if result.banned_findings:
            result.stage = "banned"
            result.error = (
                f"{len(result.banned_findings)} banned construct(s) introduced"
            )
            return result

        result.ok = True
        result.stage = "done"
        return result

    finally:
        # Cleanup sibling worktree + private hooks dir.
        cleanup_out: List[str] = []
        cleanup_ok = True
        try:
            if sibling_created:
                proc = _safe_git(
                    main_repo,
                    ["worktree", "remove", "--force", str(sibling)],
                    hooks_dir=hooks_dir,
                )
                if proc.returncode != 0:
                    cleanup_ok = False
                    cleanup_out.append(f"worktree remove: {proc.stderr.strip()}")
            if sibling.exists():
                try:
                    shutil.rmtree(sibling)
                except OSError as exc:
                    cleanup_ok = False
                    cleanup_out.append(f"rmtree: {exc}")
            prune = _safe_git(main_repo, ["worktree", "prune"], hooks_dir=hooks_dir)
            if prune.returncode != 0:
                cleanup_ok = False
                cleanup_out.append(f"prune: {prune.stderr.strip()}")
        finally:
            shutil.rmtree(hooks_dir, ignore_errors=True)

        result.cleanup_output = "\n".join(cleanup_out)
        if not cleanup_ok:
            result.cleanup_failed = True
            # Mirror CP2: if the gate would otherwise pass and cleanup
            # failed, force ok=False so callers must handle the leak.
            if result.ok:
                result.ok = False
                result.stage = "cleanup"
                result.error = f"sibling cleanup failed: {result.cleanup_output}"
