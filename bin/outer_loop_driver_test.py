#!/usr/bin/env python3
"""Tests for outer_loop_driver (CP6). Run: python3 outer_loop_driver_test.py

Covers: pure prompt/spec rendering; the track loop with a fake executor (fresh
track, reaches OBJECTIVE_COMPLETE); the give-up/blocked path; and a full
end-to-end run through the REAL CP1-CP5 pipeline + verifier_shim using a script
worker (no live claude needed — the spike already proved the claude worker).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import auto_runtime_common as rt
import outer_loop_driver as cp6
from slice_executor import ExecutorResult


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _init_repo(path: Path, files: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    for rel, content in files.items():
        (path / rel).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "seed")


def _make_track(cwd: Path, task: str, slices: list[tuple[str, str]]) -> str:
    """Init a track and give each named slice a verification command.

    slices: list of (node_id, verification_command). node_id 'slice-1' already
    exists from init; others are added.
    """
    r = rt.initialize_track(task=task, cwd=str(cwd), route_override="R2", include_memory=False)
    tid = r["track_id"]
    for nid, _ in slices:
        if nid != "slice-1":
            rt.add_slice_node(tid, f"slice {nid}", node_id=nid)
    sd = rt.track_dir(tid)
    st = json.loads((sd / "objective.state.json").read_text())
    vc_by_id = dict(slices)
    for nid, node in st["views"]["graph"]["nodes"].items():
        if nid in vc_by_id:
            node.setdefault("slice_contract", {})["verification_commands"] = [vc_by_id[nid]]
    (sd / "objective.state.json").write_text(json.dumps(st))
    return tid


class TestPureRendering(unittest.TestCase):
    def test_prompt_includes_contract_and_retry_note(self):
        node = {
            "id": "slice-1", "title": "Add feature X",
            "slice_contract": {"acceptance_criteria": ["tests pass"], "verification_commands": ["pytest -q"]},
            "owned_scope": ["src/x.py"],
        }
        first = cp6.render_worker_prompt(node, 1, None, ["pytest -q"], Path("/repo"))
        self.assertIn("Add feature X", first)
        self.assertIn("tests pass", first)
        self.assertIn("src/x.py", first)
        self.assertNotIn("previous attempt", first)
        retry = cp6.render_worker_prompt(node, 2, ExecutorResult(ok=False, stage="verify", error="assertion failed"), ["pytest -q"], Path("/repo"))
        self.assertIn("attempt 2", retry)
        self.assertIn("verify", retry)

    def test_build_spec_wires_verifier_shim(self):
        node = {"id": "slice-1", "title": "t", "owned_scope": ["a.py"], "slice_contract": {}}
        spec = cp6.build_slice_spec(node=node, slice_id="slice-1", attempt=1, last=None,
                                    verification_commands=["pytest -q", "ruff check"],
                                    worker_command=["claude", "--print"], main_repo=Path("/repo"))
        self.assertEqual(spec.verifier_command[0], "python3")
        self.assertTrue(spec.verifier_command[1].endswith("verifier_shim.py"))
        self.assertIn("--check", spec.verifier_command)
        self.assertIn("pytest -q && ruff check", spec.verifier_command)
        self.assertIn("--cite-file", spec.verifier_command)
        self.assertIn("a.py", spec.verifier_command)
        self.assertTrue(spec.branch_name.endswith("try1"))

    def test_prompt_never_leaks_absolute_main_path(self):
        # Regression (live 2026-07-15): owned_scope defaults to [cwd] (absolute
        # main-repo path); leaking it made the claude worker edit main directly.
        node = {"id": "slice-1", "title": "t", "slice_contract": {}, "owned_scope": ["/tmp/somerepo"]}
        p = cp6.render_worker_prompt(node, 1, None, ["true"], Path("/tmp/somerepo"))
        self.assertNotIn("/tmp/somerepo", p)
        self.assertIn("current working directory", p)

    def test_relative_cite_hints_drops_repo_root(self):
        hints = cp6._relative_cite_hints(["/repo", "/repo/src/a.py", "b.py", "."], Path("/repo"))
        self.assertIn("src/a.py", hints)
        self.assertIn("b.py", hints)
        self.assertNotIn("/repo", hints)
        self.assertNotIn(".", hints)

    def test_context_pack_rendered_and_capped(self):
        # S4: curated facts render into the prompt; bounds are enforced.
        pack = [f"fact {i}" for i in range(cp6.CONTEXT_PACK_MAX_ENTRIES + 3)]
        pack[0] = "the omni-mem container name is omni-mem, not omni_mem\nmulti-line"
        pack[1] = "x" * (cp6.CONTEXT_PACK_MAX_ENTRY_CHARS + 50)
        node = {"id": "slice-1", "title": "t",
                "slice_contract": {"context_pack": pack}, "owned_scope": []}
        p = cp6.render_worker_prompt(node, 1, None, ["true"], Path("/repo"))
        self.assertIn("Curated context", p)
        self.assertIn("omni-mem, not omni_mem multi-line", p)   # newline flattened
        self.assertIn("…", p)                                    # long entry truncated
        self.assertNotIn("x" * (cp6.CONTEXT_PACK_MAX_ENTRY_CHARS + 1), p)
        self.assertIn("(+3 more entries truncated)", p)          # entry cap enforced
        # No pack → no section.
        bare = cp6.render_worker_prompt(
            {"id": "s", "title": "t", "slice_contract": {}, "owned_scope": []},
            1, None, ["true"], Path("/repo"))
        self.assertNotIn("Curated context", bare)


class TestLoopWithFakeExecutor(unittest.TestCase):
    def test_three_slices_reach_objective_complete(self):
        tmp = Path(tempfile.mkdtemp(prefix="cp6loop-"))
        _init_repo(tmp, {"seed.txt": "x\n"})
        tid = _make_track(tmp, "cp6 loop fake", [("slice-1", "true"), ("slice-2", "true"), ("slice-3", "true")])

        def fake(*, main_repo, spec):
            return ExecutorResult(ok=True, stage="done", new_head_sha=f"sha-{spec.branch_name}")

        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp),
                                     capture_output=True, text=True).stdout.strip()
        summary = cp6.run_track(track_id=tid, main_repo=tmp, execute_fn=fake, sleep=lambda *_: None, max_slices=20)
        self.assertEqual(summary["closure_state"], "OBJECTIVE_COMPLETE")
        self.assertEqual(summary["accepted"], 3)
        self.assertEqual(summary["blocked"], 0)
        # S2: summary carries the review span for the post-track reviewer pass.
        self.assertEqual(summary["base_sha"], head_before)
        self.assertEqual(summary["final_sha"], head_before)  # fake executor never moves HEAD

    def test_default_verify_fallback_accepts_slice_without_explicit_verify(self):
        tmp = Path(tempfile.mkdtemp(prefix="cp6defv-"))
        _init_repo(tmp, {"seed.txt": "x\n"})
        # slice-1 from init has NO verification_commands.
        tid = rt.initialize_track(task="cp6 default-verify", cwd=str(tmp),
                                  route_override="R2", include_memory=False)["track_id"]

        def fake(*, main_repo, spec):
            return ExecutorResult(ok=True, stage="done", new_head_sha="sha")

        summary = cp6.run_track(track_id=tid, main_repo=tmp, execute_fn=fake,
                                sleep=lambda *_: None, default_verify="true", max_slices=5)
        self.assertEqual(summary["accepted"], 1, summary)
        self.assertEqual(summary["closure_state"], "OBJECTIVE_COMPLETE")

    def test_no_verify_and_no_default_blocks(self):
        tmp = Path(tempfile.mkdtemp(prefix="cp6noverify-"))
        _init_repo(tmp, {"seed.txt": "x\n"})
        tid = rt.initialize_track(task="cp6 no verify", cwd=str(tmp),
                                  route_override="R2", include_memory=False)["track_id"]

        def fake(*, main_repo, spec):
            return ExecutorResult(ok=True, stage="done", new_head_sha="sha")

        summary = cp6.run_track(track_id=tid, main_repo=tmp, execute_fn=fake,
                                sleep=lambda *_: None, max_slices=5)   # no default_verify
        self.assertEqual(summary["accepted"], 0)
        self.assertGreaterEqual(summary["blocked"], 1)

    def test_give_up_marks_blocked_and_terminates(self):
        tmp = Path(tempfile.mkdtemp(prefix="cp6block-"))
        _init_repo(tmp, {"seed.txt": "x\n"})
        tid = _make_track(tmp, "cp6 loop blocked", [("slice-1", "true")])

        def always_fail(*, main_repo, spec):
            return ExecutorResult(ok=False, stage="worker", error="derailed")

        summary = cp6.run_track(track_id=tid, main_repo=tmp, execute_fn=always_fail,
                                sleep=lambda *_: None, max_slices=10, max_attempts=3)
        self.assertEqual(summary["accepted"], 0)
        self.assertGreaterEqual(summary["blocked"], 1)
        self.assertLessEqual(summary["iterations"], 5)   # terminated, no infinite loop


class TestEndToEndRealPipeline(unittest.TestCase):
    def test_script_worker_slice_applies_to_main(self):
        tmp = Path(tempfile.mkdtemp(prefix="cp6e2e-"))
        _init_repo(tmp, {"marker.txt": "TODO\n"})
        # Worker: change marker.txt TODO -> DONE (prompt appended as $1, ignored).
        worker = tmp.parent / f"worker-{tmp.name}.sh"
        worker.write_text("#!/usr/bin/env bash\nset -e\necho DONE > marker.txt\n")
        tid = _make_track(tmp, "cp6 e2e script worker", [("slice-1", "grep -q DONE marker.txt")])

        summary = cp6.run_track(
            track_id=tid, main_repo=tmp,
            worker_command=["bash", str(worker)],
            sleep=lambda *_: None, max_slices=5,
        )
        self.assertEqual(summary["closure_state"], "OBJECTIVE_COMPLETE", summary)
        self.assertEqual(summary["accepted"], 1, summary)
        # The accepted slice's diff was ff-merged into main.
        self.assertEqual((tmp / "marker.txt").read_text().strip(), "DONE")
        # S2: real merge moved HEAD — review span is non-empty and ordered.
        self.assertIsNotNone(summary["base_sha"])
        self.assertIsNotNone(summary["final_sha"])
        self.assertNotEqual(summary["base_sha"], summary["final_sha"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
