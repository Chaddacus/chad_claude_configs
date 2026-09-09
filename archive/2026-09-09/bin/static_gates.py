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

3. **Stub bodies (AST, .py)**: a function whose ENTIRE body (after an
   optional docstring) is `pass`, `...`, or `raise NotImplementedError`
   is unimplemented work pretending to be done. Blocked when the
   function's line range intersects the diff's added lines (so gutting
   an existing function into a stub is caught too). Exempt: functions
   decorated `@abstractmethod`/`@overload` and methods of Protocol
   classes — those bodies are stubs by design.

4. **Cheat/stub line patterns (regex, all text files)**: scans the ADDED
   lines of every changed text file (not just .py):
   - hard-fail: `todo!()`/`unimplemented!()` (Rust), "not implemented"
     throws (JS/TS), and — in TEST files only — newly added skip/only/
     xfail annotations (`it.skip`, `.only(`, `@pytest.mark.skip[if]`,
     `@pytest.mark.xfail`, `pytest.skip(`, `skipTest(`) which neuter the
     verification the pipeline's acceptance rests on.
   - report-only (`GateResult.warning_findings`, never blocks):
     TODO/FIXME/XXX markers, `NODE_ENV === "test"` branches, and
     docstring-only function bodies.

Out of scope
------------
- Type checks (mypy/pyright), style (black/ruff), test coverage.
- Project-specific patterns; CP3 enforces only universal anti-patterns.
- AST-level checks for languages other than Python (non-Python files get
  the added-line regex pass only).
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

# NOTE: A regex pass was tried in CP3 R1 alongside the AST visitor for
# the BANNED-BUILTIN checks and dropped (AST is comprehensive there and
# regex false-positives on `def eval(self):`). The regex layer below is
# a DIFFERENT concern: stub/cheat line patterns that have no Python AST
# shape (Rust macros, JS throws, test-skip annotations) scanned over the
# diff's added lines only — a scope where false positives are rare and
# a retry with feedback is cheap.


# --- Stub / cheat detection tables (S1, fleet-hardening) ---------------

# Test-file detection for patterns that only make sense in tests (skip/
# only annotations). Mirrors the conventions test-strategist enforces.
_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__)/"
    r"|(^|/)conftest\.py$"
    r"|(^|/)test_[^/]+\.py$"
    r"|_test\.py$"
    r"|\.(test|spec)\.[cm]?[jt]sx?$"
    r"|_test\.go$"
    r"|_spec\.rb$"
)

# (compiled_pattern, pattern_name, rationale, test_files_only)
_HARD_LINE_PATTERNS: List[Tuple[re.Pattern, str, str, bool]] = [
    (re.compile(r"\btodo!\s*\("), "rust_todo_macro",
     "todo!() placeholder left in added code", False),
    (re.compile(r"\bunimplemented!\s*\("), "rust_unimplemented_macro",
     "unimplemented!() placeholder left in added code", False),
    (re.compile(r"throw\s+new\s+Error\s*\(\s*['\"](?:not\s+implemented|unimplemented|todo)", re.I),
     "js_not_implemented_throw", "'not implemented' throw left in added code", False),
    (re.compile(r"\b(?:it|test|describe|suite)\s*\.\s*only\s*\("), "test_only_added",
     "focused test (.only) neuters the rest of the suite", True),
    (re.compile(r"\b(?:it|test|describe|suite)\s*\.\s*skip\s*\("), "test_skip_added",
     "skipped test added — verification neutered", True),
    (re.compile(r"\bx(?:it|describe)\s*\("), "test_skip_added",
     "xit/xdescribe skipped test added — verification neutered", True),
    (re.compile(r"@pytest\.mark\.(?:skip|skipif|xfail)\b"), "pytest_skip_added",
     "pytest skip/xfail added — verification neutered", True),
    (re.compile(r"@unittest\.skip"), "unittest_skip_added",
     "unittest skip added — verification neutered", True),
    (re.compile(r"\bpytest\.skip\s*\(|\bskipTest\s*\("), "runtime_skip_added",
     "runtime test-skip call added — verification neutered", True),
]

# Report-only: recorded in GateResult.warning_findings, never blocks.
_WARNING_LINE_PATTERNS: List[Tuple[re.Pattern, str, str, bool]] = [
    (re.compile(r"\b(?:TODO|FIXME|XXX)\b"), "todo_marker",
     "TODO/FIXME marker in added lines", False),
    (re.compile(r"NODE_ENV\s*===?\s*['\"]test['\"]"), "node_env_test_branch",
     "test-env conditional in added code — check it isn't a verification bypass", False),
]

# Decorators that legitimately produce stub-shaped bodies.
_STUB_EXEMPT_DECORATORS = {"abstractmethod", "overload"}


def _decorator_tail(dec: ast.expr) -> str:
    """Last attribute segment of a decorator expression: `abc.abstractmethod`
    -> 'abstractmethod', `overload` -> 'overload', `foo()` -> 'foo'."""
    if isinstance(dec, ast.Call):
        return _decorator_tail(dec.func)
    if isinstance(dec, ast.Attribute):
        return dec.attr
    if isinstance(dec, ast.Name):
        return dec.id
    return ""


def _base_is_protocol(base: ast.expr) -> bool:
    """True for bases spelled Protocol / typing.Protocol / Protocol[T]."""
    if isinstance(base, ast.Subscript):
        return _base_is_protocol(base.value)
    if isinstance(base, ast.Attribute):
        return base.attr == "Protocol"
    if isinstance(base, ast.Name):
        return base.id == "Protocol"
    return False


def _raises_not_implemented(stmt: ast.Raise) -> bool:
    exc = stmt.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    return isinstance(exc, ast.Name) and exc.id in ("NotImplementedError", "NotImplemented")


def _walk_ast_stubs(tree: ast.AST) -> List[Tuple[int, int, str, str, bool]]:
    """Find stub-shaped function bodies.

    Returns (lineno, end_lineno, pattern_name, rationale, is_warning).
    Hard stubs: body (docstring aside) is exactly `pass` / `...` /
    `raise NotImplementedError`. Warning: docstring-only body.
    Exempt: @abstractmethod/@overload functions and Protocol-class methods.
    """
    protocol_methods: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(_base_is_protocol(b) for b in node.bases):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    protocol_methods.add(sub.lineno)

    out: List[Tuple[int, int, str, str, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.lineno in protocol_methods:
            continue
        if any(_decorator_tail(d) in _STUB_EXEMPT_DECORATORS for d in node.decorator_list):
            continue
        body = list(node.body)
        has_doc = (
            bool(body)
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        )
        rest = body[1:] if has_doc else body
        end = getattr(node, "end_lineno", None) or node.lineno
        if not rest:
            out.append((node.lineno, end, "docstring_only_body",
                        "function body is only a docstring — possible stub", True))
            continue
        if len(rest) != 1:
            continue
        stmt = rest[0]
        if isinstance(stmt, ast.Pass):
            out.append((node.lineno, end, "stub_pass_body",
                        "function body is only `pass` — unimplemented stub", False))
        elif (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
              and stmt.value.value is Ellipsis):
            out.append((node.lineno, end, "stub_ellipsis_body",
                        "function body is only `...` — unimplemented stub", False))
        elif isinstance(stmt, ast.Raise) and _raises_not_implemented(stmt):
            out.append((node.lineno, end, "stub_not_implemented",
                        "function body only raises NotImplementedError — unimplemented stub", False))
    return out


def _scan_lines_regex(
    path_disp: str,
    file_lines: List[str],
    in_scope: Optional[Set[int]],
) -> Tuple[List[Tuple[int, str, str]], List[Tuple[int, str, str]]]:
    """Run the hard/warning line-pattern tables over the in-scope lines.

    `in_scope=None` means every line (new file). Returns
    (hard_findings, warning_findings) as (lineno, pattern_name, rationale).
    """
    is_test = bool(_TEST_PATH_RE.search(path_disp))
    linenos = range(1, len(file_lines) + 1) if in_scope is None else sorted(in_scope)
    hard: List[Tuple[int, str, str]] = []
    warn: List[Tuple[int, str, str]] = []
    for ln in linenos:
        if ln < 1 or ln > len(file_lines):
            continue
        text = file_lines[ln - 1]
        for pat, name, rationale, test_only in _HARD_LINE_PATTERNS:
            if test_only and not is_test:
                continue
            if pat.search(text):
                hard.append((ln, name, rationale))
        for pat, name, rationale, test_only in _WARNING_LINE_PATTERNS:
            if test_only and not is_test:
                continue
            if pat.search(text):
                warn.append((ln, name, rationale))
    return hard, warn


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
    source: str  # "ast" (banned builtins, stub bodies) or "regex" (line patterns)


@dataclass
class GateResult:
    ok: bool
    stage: str
    error: Optional[str] = None
    parse_errors: List[ParseError] = field(default_factory=list)
    banned_findings: List[BannedFinding] = field(default_factory=list)
    # Report-only findings (TODO markers, docstring-only bodies, env-test
    # branches). Never affect `ok`; surfaced for the supervisor to read.
    warning_findings: List[BannedFinding] = field(default_factory=list)
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


@dataclass
class _FileScan:
    """One non-deleted changed file, read once and scoped once, consumed by
    both the AST pass (.py) and the regex line-pattern pass (all files)."""
    ch: _ChangedFile
    path_disp: str
    raw: Optional[bytes]            # None if the post-apply file couldn't be read
    read_error: Optional[str]
    added: Optional[Set[int]]       # git's added-line set; None = whole file (new file)


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

        # Read every non-deleted changed file ONCE and compute its added-
        # line scope ONCE. Deletions are skipped (no post-apply file).
        # The AST pass (.py only) and the regex line-pattern pass (all
        # text files) both consume these records.
        scans: List[_FileScan] = []
        for ch in changes:
            if ch.status == "D":
                continue
            full_bytes = os.fsencode(str(sibling)) + b"/" + ch.path
            path_disp = os.fsdecode(ch.path)
            raw: Optional[bytes] = None
            read_error: Optional[str] = None
            try:
                with open(full_bytes, "rb") as fh:
                    raw = fh.read()
            except OSError as exc:
                read_error = str(exc)
            if ch.status == "A":
                added: Optional[Set[int]] = None  # brand-new file: all lines in scope
            else:
                try:
                    added = _added_line_set_for_file(
                        sibling, ch.path,
                        old_path=(ch.old_path if ch.status in ("R", "C") else None),
                    )
                except SandboxError as exc:
                    result.stage = "apply"
                    result.error = f"failed to compute added lines: {exc}"
                    return result
            scans.append(_FileScan(ch=ch, path_disp=path_disp, raw=raw,
                                   read_error=read_error, added=added))

        py_scans = [s for s in scans if s.ch.path.endswith(b".py")]
        result.files_checked = [s.path_disp for s in py_scans]

        # Parse each Python file (from the cached bytes).
        parsed_trees: List[Tuple[_FileScan, Optional[ast.AST], List[str]]] = []
        for scan in py_scans:
            if scan.raw is None:
                result.parse_errors.append(
                    ParseError(path=scan.path_disp,
                               message=f"read failed: {scan.read_error}",
                               lineno=None, offset=None)
                )
                parsed_trees.append((scan, None, []))
                continue
            try:
                content = scan.raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                result.parse_errors.append(
                    ParseError(
                        path=scan.path_disp,
                        message=f"not valid UTF-8: {exc}",
                        lineno=None,
                        offset=None,
                    )
                )
                parsed_trees.append((scan, None, []))
                continue
            try:
                tree = ast.parse(content, filename=scan.path_disp)
            except SyntaxError as exc:
                result.parse_errors.append(
                    ParseError(
                        path=scan.path_disp,
                        message=str(exc.msg or "syntax error"),
                        lineno=exc.lineno,
                        offset=exc.offset,
                    )
                )
                parsed_trees.append((scan, None, content.split("\n")))
                continue
            parsed_trees.append((scan, tree, content.split("\n")))

        # Banned-construct + stub inspection (.py, AST).
        for scan, tree, source_lines in parsed_trees:
            ch = scan.ch
            path_disp = scan.path_disp
            # Effective "in-scope lines" for the AST pass:
            # - status "A": brand-new file → all lines in scope.
            # - status "R"/"C" from a non-.py old path: the file BECOMES
            #   Python here → whole file in scope.
            # - otherwise: the added-line set computed in the pre-pass.
            in_scope_lines: Optional[Set[int]] = scan.added
            if ch.status in ("R", "C"):
                old_is_py = ch.old_path is not None and ch.old_path.endswith(b".py")
                if not old_is_py:
                    in_scope_lines = None  # Becoming Python — whole file in scope.

            if tree is None:
                continue
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
            # Stub-shaped bodies: flag when the function's line RANGE
            # intersects the added set, so gutting an existing function
            # into `pass` is caught, not just brand-new stubs.
            for lineno, end_lineno, name, rationale, is_warning in _walk_ast_stubs(tree):
                if in_scope_lines is not None and not (
                    set(range(lineno, end_lineno + 1)) & in_scope_lines
                ):
                    continue
                finding = BannedFinding(
                    path=path_disp,
                    lineno=lineno,
                    pattern_name=name,
                    rationale=rationale,
                    source="ast",
                )
                (result.warning_findings if is_warning
                 else result.banned_findings).append(finding)

        # Line-pattern pass (regex) over the added lines of EVERY changed
        # text file — stubs/cheats with no Python AST shape. Files that
        # can't be read or aren't valid UTF-8 (binaries) are skipped;
        # unreadable .py files were already reported as parse errors.
        for scan in scans:
            if scan.raw is None:
                continue
            try:
                file_lines = scan.raw.decode("utf-8").split("\n")
            except UnicodeDecodeError:
                continue  # binary — no line patterns to scan
            hard, warn = _scan_lines_regex(scan.path_disp, file_lines, scan.added)
            for lineno, name, rationale in hard:
                result.banned_findings.append(
                    BannedFinding(path=scan.path_disp, lineno=lineno,
                                  pattern_name=name, rationale=rationale,
                                  source="regex")
                )
            for lineno, name, rationale in warn:
                result.warning_findings.append(
                    BannedFinding(path=scan.path_disp, lineno=lineno,
                                  pattern_name=name, rationale=rationale,
                                  source="regex")
                )

        if result.parse_errors:
            result.stage = "parse"
            head = "; ".join(
                f"{p.path}:{p.lineno or '?'} {p.message}" for p in result.parse_errors[:5]
            )
            result.error = (
                f"{len(result.parse_errors)} parse error(s) in changed Python files: {head}"
            )
            return result
        if result.banned_findings:
            # Name the findings in the error so CP7's retry prompt tells the
            # next worker WHAT was wrong, not just that something was.
            result.stage = "banned"
            head = "; ".join(
                f"{f.path}:{f.lineno} {f.pattern_name}" for f in result.banned_findings[:8]
            )
            more = (f" (+{len(result.banned_findings) - 8} more)"
                    if len(result.banned_findings) > 8 else "")
            result.error = (
                f"{len(result.banned_findings)} banned construct(s) introduced: {head}{more}"
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
