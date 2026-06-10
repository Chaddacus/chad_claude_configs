"""Tests for static_gates.static_gate.

Run from /Users/chadsimon/.claude/bin/:
    python3 -m unittest static_gates_test
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import static_gates
from static_gates import static_gate


def _run(cmd, cwd, **kwargs):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=True, **kwargs)


def _init_repo(path: Path, files: dict) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], cwd=path)
    _run(["git", "config", "user.email", "test@local"], cwd=path)
    _run(["git", "config", "user.name", "Test"], cwd=path)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=path)
    for relpath, content in files.items():
        target = path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _run(["git", "add", relpath], cwd=path)
    _run(["git", "commit", "-q", "-m", "init"], cwd=path)
    return _run(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()


def _make_diff(repo: Path, mutate) -> bytes:
    """Apply `mutate(tmp_path)` in a throwaway worktree at HEAD, return cached diff bytes."""
    tmp = repo.parent / f"_difftmp-{os.urandom(4).hex()}"
    _run(["git", "worktree", "add", "--detach", str(tmp), "HEAD"], cwd=repo)
    try:
        mutate(tmp)
        _run(["git", "add", "-A"], cwd=tmp)
        proc = subprocess.run(
            ["git", "diff", "--cached", "--binary"],
            cwd=str(tmp),
            capture_output=True,
            check=True,
        )
        return proc.stdout
    finally:
        _run(["git", "worktree", "remove", "--force", str(tmp)], cwd=repo)


class StaticGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cp3-"))
        self.repo = self.tmp / "main"
        self.base_sha = _init_repo(
            self.repo,
            {"app.py": "def foo():\n    return 1\n", "README.md": "# repo\n"},
        )
        self.parent = self.tmp / "gate"

    def tearDown(self) -> None:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(self.repo) if self.repo.exists() else self.tmp,
            capture_output=True,
        )
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- happy paths --------------------------------------------------

    def test_clean_modification_passes(self) -> None:
        def mutate(tmp):
            (tmp / "app.py").write_text("def foo():\n    return 2\n")
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertTrue(result.ok, f"{result.stage}: {result.error}")
        self.assertEqual(result.stage, "done")
        self.assertEqual(result.files_checked, ["app.py"])
        self.assertEqual(result.parse_errors, [])
        self.assertEqual(result.banned_findings, [])
        # Sibling cleaned up.
        self.assertFalse(Path(result.sibling_path).exists())

    def test_non_python_file_skipped(self) -> None:
        def mutate(tmp):
            (tmp / "README.md").write_text("# repo\n\nnew text with eval( in markdown\n")
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertTrue(result.ok, f"{result.stage}: {result.error}")
        self.assertEqual(result.files_checked, [])

    def test_added_python_file_passes(self) -> None:
        def mutate(tmp):
            (tmp / "new.py").write_text("def bar():\n    return 'ok'\n")
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertTrue(result.ok, f"{result.stage}: {result.error}")
        self.assertIn("new.py", result.files_checked)

    def test_deleted_python_file_not_parsed(self) -> None:
        def mutate(tmp):
            (tmp / "app.py").unlink()
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertTrue(result.ok, f"{result.stage}: {result.error}")
        # Deleted file isn't parsed (it doesn't exist post-apply).
        self.assertNotIn("app.py", result.files_checked)

    # ---- parse errors -------------------------------------------------

    def test_syntax_error_rejected(self) -> None:
        def mutate(tmp):
            (tmp / "app.py").write_text("def broken(:\n    return 1\n")
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "parse")
        self.assertEqual(len(result.parse_errors), 1)
        self.assertEqual(result.parse_errors[0].path, "app.py")
        self.assertIsNotNone(result.parse_errors[0].lineno)

    def test_added_python_file_with_syntax_error(self) -> None:
        def mutate(tmp):
            (tmp / "bad.py").write_text("def x(\n")
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "parse")
        self.assertEqual(result.parse_errors[0].path, "bad.py")

    # ---- banned constructs --------------------------------------------

    def test_eval_call_rejected(self) -> None:
        def mutate(tmp):
            (tmp / "app.py").write_text("def foo():\n    return eval('1+1')\n")
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "banned")
        self.assertTrue(any(f.pattern_name == "eval_call" for f in result.banned_findings))

    def test_exec_call_rejected(self) -> None:
        def mutate(tmp):
            (tmp / "app.py").write_text("def foo():\n    exec('print(1)')\n    return 1\n")
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "banned")
        self.assertTrue(any(f.pattern_name == "exec_call" for f in result.banned_findings))

    def test_shell_true_rejected(self) -> None:
        def mutate(tmp):
            (tmp / "app.py").write_text(
                "import subprocess\n"
                "def foo(x):\n"
                "    subprocess.run(x, shell=True)\n"
                "    return 1\n"
            )
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "banned")
        self.assertTrue(any(f.pattern_name == "shell_true" for f in result.banned_findings))

    def test_bare_except_rejected(self) -> None:
        def mutate(tmp):
            (tmp / "app.py").write_text(
                "def foo():\n"
                "    try:\n"
                "        return 1\n"
                "    except:\n"
                "        return 0\n"
            )
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "banned")
        self.assertTrue(any(f.pattern_name == "bare_except" for f in result.banned_findings))

    def test_wildcard_import_rejected(self) -> None:
        def mutate(tmp):
            (tmp / "app.py").write_text("from os import *\n\ndef foo():\n    return 1\n")
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "banned")
        self.assertTrue(any(f.pattern_name == "wildcard_import" for f in result.banned_findings))

    def test_dunder_import_rejected(self) -> None:
        def mutate(tmp):
            (tmp / "app.py").write_text("def foo(m):\n    return __import__(m)\n")
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "banned")
        self.assertTrue(any(f.pattern_name == "dunder_import" for f in result.banned_findings))

    def test_compile_call_rejected(self) -> None:
        def mutate(tmp):
            (tmp / "app.py").write_text("def foo(src):\n    return compile(src, '<x>', 'exec')\n")
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "banned")
        self.assertTrue(any(f.pattern_name == "compile_call" for f in result.banned_findings))

    def test_banned_in_pre_existing_lines_not_flagged(self) -> None:
        """Banned constructs that exist in the BASE file but the diff
        doesn't touch them shouldn't be flagged."""
        # Init with eval already in the file.
        bad_repo = self.tmp / "bad-base"
        bad_base = _init_repo(
            bad_repo,
            {"app.py": "def foo():\n    return eval('1')\n"},  # eval pre-existing
        )

        def mutate(tmp):
            (tmp / "app.py").write_text(
                "def foo():\n    return eval('1')\n\n"
                "def bar():\n    return 2\n"  # only this is new
            )
        diff = _make_diff(bad_repo, mutate)
        result = static_gate(
            main_repo=bad_repo,
            base_sha=bad_base,
            diff_bytes=diff,
            rehearsal_parent=self.tmp / "gate-bad",
        )
        # Parse passes (file is valid Python). Banned check only flags
        # ADDED lines, and the new lines don't contain eval. So OK.
        self.assertTrue(result.ok, f"{result.stage}: {result.error}")

    def test_banned_in_non_python_file_ignored(self) -> None:
        """eval(...) inside a Markdown file is not a violation."""
        def mutate(tmp):
            (tmp / "doc.md").write_text("Do not use eval(x). It's banned.\n")
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertTrue(result.ok, f"{result.stage}: {result.error}")

    def test_multiple_banned_findings_collected(self) -> None:
        def mutate(tmp):
            (tmp / "app.py").write_text(
                "def foo():\n"
                "    x = eval('1')\n"
                "    y = exec('print(1)')\n"
                "    return x\n"
            )
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "banned")
        names = {f.pattern_name for f in result.banned_findings}
        self.assertIn("eval_call", names)
        self.assertIn("exec_call", names)

    # ---- failure modes ------------------------------------------------

    def test_empty_diff_rejected(self) -> None:
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=b"",
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "empty")

    def test_malformed_diff_rejected_at_apply(self) -> None:
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=b"not a real diff\n",
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "apply")

    def test_diff_against_wrong_base_fails_apply(self) -> None:
        # Mutate the file in main so the diff (generated against original
        # base) won't apply cleanly against a different base.
        diff = _make_diff(
            self.repo, lambda tmp: (tmp / "app.py").write_text("def foo():\n    return 99\n")
        )
        # Change app.py in main and commit, so diff is no longer applicable.
        (self.repo / "app.py").write_text("totally different\n")
        _run(["git", "add", "app.py"], cwd=self.repo)
        _run(["git", "commit", "-q", "-m", "drift"], cwd=self.repo)
        new_base = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()

        result = static_gate(
            main_repo=self.repo,
            base_sha=new_base,  # diff was generated against an older base
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "apply")

    def test_main_untouched_after_all_failure_paths(self) -> None:
        scenarios = []
        scenarios.append(("syntax",
                          _make_diff(self.repo, lambda tmp: (tmp / "app.py").write_text("def x(:\n"))))
        scenarios.append(("banned",
                          _make_diff(self.repo, lambda tmp: (tmp / "app.py").write_text("def x():\n    return eval('1')\n"))))
        scenarios.append(("apply", b"not a diff\n"))

        for name, diff in scenarios:
            with self.subTest(scenario=name):
                head_before = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
                content_before = (self.repo / "app.py").read_text()
                result = static_gate(
                    main_repo=self.repo,
                    base_sha=self.base_sha,
                    diff_bytes=diff,
                    rehearsal_parent=self.parent,
                )
                self.assertFalse(result.ok)
                head_after = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
                content_after = (self.repo / "app.py").read_text()
                self.assertEqual(head_before, head_after)
                self.assertEqual(content_before, content_after)

    # ---- helpers ------------------------------------------------------

    # ---- R1 regression: C-quoted paths, AST visitor, renames -----

    def test_unicode_python_path_is_gated(self) -> None:
        """A .py file with a non-ASCII name (C-quoted in diff headers)
        must be parsed and banned-scanned, NOT silently skipped."""
        def mutate(tmp):
            (tmp / "unicodé.py").write_text("def x():\n    return eval('1')\n")
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        # The file should be checked AND should be rejected for eval.
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "banned")
        self.assertTrue(any("unicod" in p for p in result.files_checked))
        self.assertTrue(any("eval" in f.pattern_name for f in result.banned_findings))

    def test_rename_from_txt_to_py_triggers_full_scan(self) -> None:
        """Renaming a non-Python file to a .py file should treat the
        ENTIRE post-apply content as introduced, so banned constructs
        anywhere in it are flagged."""
        # Set up a repo where payload.txt exists with eval('1') in it.
        bad_repo = self.tmp / "rename-base"
        bad_base = _init_repo(
            bad_repo,
            {"payload.txt": "def x():\n    return eval('1')\n"},
        )

        # Diff: rename payload.txt → payload.py (no content change).
        tmp = bad_repo.parent / f"_ren-{os.urandom(4).hex()}"
        _run(["git", "worktree", "add", "--detach", str(tmp), "HEAD"], cwd=bad_repo)
        try:
            _run(["git", "mv", "payload.txt", "payload.py"], cwd=tmp)
            diff = subprocess.run(
                ["git", "diff", "--cached", "--binary"],
                cwd=str(tmp), capture_output=True, check=True
            ).stdout
        finally:
            _run(["git", "worktree", "remove", "--force", str(tmp)], cwd=bad_repo)

        result = static_gate(
            main_repo=bad_repo,
            base_sha=bad_base,
            diff_bytes=diff,
            rehearsal_parent=self.tmp / "gate-ren",
        )
        self.assertFalse(result.ok, f"expected ban but got {result.stage}: {result.error}")
        self.assertEqual(result.stage, "banned")
        self.assertTrue(any("eval" in f.pattern_name for f in result.banned_findings))

    def test_ast_catches_eval_via_name_alias(self) -> None:
        """`fn = eval; fn(x)` evades a literal `eval(` regex but the AST
        visitor catches it via Name(id='eval', ctx=Load)."""
        def mutate(tmp):
            (tmp / "app.py").write_text(
                "def foo(x):\n"
                "    fn = eval\n"
                "    return fn(x)\n"
            )
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "banned")
        self.assertTrue(
            any(f.pattern_name == "eval_call" and f.source == "ast"
                for f in result.banned_findings)
        )

    def test_ast_catches_shell_true_via_keyword(self) -> None:
        """The AST shell=True keyword check fires even on indirect call
        targets like `subprocess.run` vs `run` alias."""
        def mutate(tmp):
            (tmp / "app.py").write_text(
                "import subprocess as sp\n"
                "def foo(x):\n"
                "    sp.run(x, shell=True)\n"
                "    return 1\n"
            )
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok)
        self.assertTrue(any(f.pattern_name == "shell_true" for f in result.banned_findings))

    def test_obj_eval_method_not_falsely_flagged(self) -> None:
        """`obj.eval(x)` is a method call, NOT a builtin reference. The
        AST visitor must not flag it (different ctx/path). The tightened
        regex with (?<![\\w.]) also avoids the false positive."""
        def mutate(tmp):
            (tmp / "app.py").write_text(
                "class C:\n"
                "    def eval(self, x):\n"
                "        return x\n"
                "def foo(c, x):\n"
                "    return c.eval(x)\n"
            )
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        # Should pass: c.eval is method call, not builtin reference.
        self.assertTrue(result.ok, f"{result.stage}: {result.banned_findings}")

    def test_myshell_true_not_falsely_flagged(self) -> None:
        """`myshell=True` is a parameter name, not the subprocess shell
        kwarg. The tightened regex should not flag it."""
        def mutate(tmp):
            (tmp / "app.py").write_text(
                "def foo(myshell=True):\n"
                "    return myshell\n"
            )
        diff = _make_diff(self.repo, mutate)
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertTrue(result.ok, f"{result.stage}: {result.banned_findings}")

    # ---- R2 regression: hunk content with leading +/-, rename-within-py ----

    def test_added_line_starting_with_plus_plus_doesnt_break_scope(self) -> None:
        """A diff whose content line starts with `+` (rendered as `++` in
        the unified diff stream) must NOT be treated as a file header by
        the line-scope parser. The subsequent eval('1') on a later line
        must be caught.

        Pre-R2 fix: `_added_line_set_for_file` exited the hunk when it
        saw `+++x` (a valid Python prefix-expression like ++x). That
        caused later added lines including eval() to be missed.
        """
        def mutate(tmp):
            # Add a line that begins with `+` (so the diff carries `++`
            # for the line content) and then a banned eval call.
            (tmp / "app.py").write_text(
                "def foo():\n"
                "    return 1\n"
                "\n"
                "x = 0\n"
                "+x  # noop but valid syntax: x  prefixed with +\n"
                "result = eval('1')\n"
            )
        diff = _make_diff(self.repo, mutate)
        # Sanity: diff text should contain a line starting with `++`.
        self.assertIn(b"\n++", diff, "test fixture didn't produce the ++ pattern")
        result = static_gate(
            main_repo=self.repo,
            base_sha=self.base_sha,
            diff_bytes=diff,
            rehearsal_parent=self.parent,
        )
        self.assertFalse(result.ok, f"{result.stage}: {result.error}")
        self.assertEqual(result.stage, "banned")
        # eval must be flagged.
        self.assertTrue(
            any(f.pattern_name == "eval_call" for f in result.banned_findings),
            f"eval not flagged; findings={result.banned_findings}",
        )

    def test_rename_within_py_does_not_flag_preexisting_banned(self) -> None:
        """Pure rename within .py (with content change) must NOT flag
        pre-existing banned constructs that weren't added by this diff.

        Pre-R2 fix: `git diff -U0 HEAD -- new.py` without -M+old-path
        treated new.py as fully new, scoping every line in.
        """
        # Repo: old.py has pre-existing eval('1').
        bad_repo = self.tmp / "ren-within-py"
        bad_base = _init_repo(
            bad_repo,
            {"old.py": "def x():\n    return eval('1')\n"},
        )

        # Rename old.py → new.py AND add a safe function.
        tmp = bad_repo.parent / f"_ren-{os.urandom(4).hex()}"
        _run(["git", "worktree", "add", "--detach", str(tmp), "HEAD"], cwd=bad_repo)
        try:
            _run(["git", "mv", "old.py", "new.py"], cwd=tmp)
            (tmp / "new.py").write_text(
                "def x():\n    return eval('1')\n\n"
                "def safe():\n    return 42\n"
            )
            _run(["git", "add", "-A"], cwd=tmp)
            diff = subprocess.run(
                ["git", "diff", "--cached", "--binary", "-M"],
                cwd=str(tmp), capture_output=True, check=True
            ).stdout
        finally:
            _run(["git", "worktree", "remove", "--force", str(tmp)], cwd=bad_repo)

        result = static_gate(
            main_repo=bad_repo,
            base_sha=bad_base,
            diff_bytes=diff,
            rehearsal_parent=self.tmp / "gate-renwithin",
        )
        # Pre-existing eval should NOT be flagged. The diff only added
        # a safe function.
        self.assertTrue(result.ok, f"unexpected ban: {result.stage}: {result.banned_findings}")

    def test_cleanup_failure_forces_ok_false(self) -> None:
        """If sibling cleanup fails, ok must be False with stage='cleanup'
        (mirrors CP2 contract)."""
        def mutate(tmp):
            (tmp / "app.py").write_text("def x():\n    return 2\n")
        diff = _make_diff(self.repo, mutate)

        from unittest import mock
        real_run = subprocess.run

        def cleanup_breaking_run(cmd, *args, **kwargs):
            if (
                isinstance(cmd, list)
                and "worktree" in cmd
                and "remove" in cmd
            ):
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="injected failure"
                )
            return real_run(cmd, *args, **kwargs)

        def broken_rmtree(*args, **kwargs):
            # Honor ignore_errors=True so the outer hooks-dir cleanup
            # doesn't propagate (it's called with ignore_errors=True
            # and real shutil.rmtree would silently absorb the failure).
            if kwargs.get("ignore_errors"):
                return
            raise OSError(13, "injected rmtree failure")

        with mock.patch.object(static_gates.subprocess, "run", side_effect=cleanup_breaking_run), \
             mock.patch.object(static_gates.shutil, "rmtree", side_effect=broken_rmtree):
            result = static_gate(
                main_repo=self.repo,
                base_sha=self.base_sha,
                diff_bytes=diff,
                rehearsal_parent=self.parent,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "cleanup")
        self.assertTrue(result.cleanup_failed)
        # Cleanup the leaked sibling so tearDown succeeds.
        subprocess.run(
            ["git", "worktree", "remove", "--force", result.sibling_path],
            cwd=str(self.repo), capture_output=True,
        )
        if Path(result.sibling_path).exists():
            shutil.rmtree(result.sibling_path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
