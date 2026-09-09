#!/usr/bin/env python3
"""Shadow-mode streaming gate with completion_gate semantics.

Verifies with the SAME checks completion_gate would run (the project's resolved
test/lint/typecheck commands), not a weak per-file syntax check — so the shadow
comparison (streamed vs real) is apples-to-apples.

Model (mirrors completion_gate's "verify the project after edits"):
  PostToolUse(Edit|Write) -> record_edit(): note the edit, then launch a
     background project verify keyed by a TREE SIGNATURE (the set+content of the
     session's edited files under that project root). At most one verify in
     flight per root (newer edits supersede/kill the older run).
  Stop -> evaluate(): for the CURRENT tree signature, return the cached verdict
     (PASS/FAIL/NO_COMMANDS) or INCONCLUSIVE (still running) / MISS (never ran).

NOTE: find_project_root / resolve_commands / run_command MIRROR
completion_gate.py. completion_gate runs should_run()+sys.exit() at import time,
so it cannot be imported safely from a background process. Consolidate both into
one shared module at cutover (tracked by redundancy_report.py).

Config flag (dandori/config.json -> streaming_gates): off | shadow | on.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path("/Users/chadsimon/.claude")
CONFIG = HOME / "dandori" / "config.json"
CACHE_ROOT = HOME / "state" / "dandori" / "stream"
RUNNER = HOME / "dandori" / "bin" / "stream_verify_project.py"

# ---- mirrors completion_gate.py (consolidate at cutover) -------------------
CODE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".pyw", ".rs", ".go",
    ".java", ".kt", ".kts", ".rb", ".c", ".cpp", ".cc", ".h", ".hpp", ".cs",
    ".swift", ".sh", ".bash", ".zsh",
}
MARKERS = ["package.json", "Cargo.toml", "go.mod", "pyproject.toml",
           "setup.py", "Makefile", ".git"]


def find_project_root(start_dir: str) -> str:
    path = os.path.abspath(start_dir)
    while path != "/":
        for m in MARKERS:
            if os.path.exists(os.path.join(path, m)):
                return path
        path = os.path.dirname(path)
    return os.path.abspath(start_dir)


def resolve_commands(root: str):
    cmds = []
    pkg = os.path.join(root, "package.json")
    if os.path.exists(pkg):
        try:
            scripts = json.loads(open(pkg).read()).get("scripts", {})
            if "test" in scripts:
                cmds.append({"cmd": "npm test", "label": "tests"})
            if "typecheck" in scripts:
                cmds.append({"cmd": "npm run typecheck", "label": "typecheck"})
            elif "check" in scripts:
                cmds.append({"cmd": "npm run check", "label": "check"})
            elif os.path.exists(os.path.join(root, "tsconfig.json")):
                cmds.append({"cmd": "npx tsc --noEmit", "label": "typecheck"})
        except Exception:
            pass
    if os.path.exists(os.path.join(root, "pyproject.toml")):
        try:
            c = open(os.path.join(root, "pyproject.toml")).read()
            if "[tool.pytest" in c:
                cmds.append({"cmd": "python -m pytest", "label": "tests"})
            if "ruff" in c:
                cmds.append({"cmd": "ruff check .", "label": "lint"})
        except Exception:
            pass
    if os.path.exists(os.path.join(root, "Cargo.toml")):
        cmds += [{"cmd": "cargo test", "label": "tests"}, {"cmd": "cargo clippy", "label": "lint"}]
    if os.path.exists(os.path.join(root, "go.mod")):
        cmds += [{"cmd": "go test ./...", "label": "tests"}, {"cmd": "go vet ./...", "label": "lint"}]
    if not cmds and os.path.exists(os.path.join(root, "Makefile")):
        try:
            c = open(os.path.join(root, "Makefile")).read()
            if "\ntest:" in c or c.startswith("test:"):
                cmds.append({"cmd": "make test", "label": "tests"})
            if "\ncheck:" in c:
                cmds.append({"cmd": "make check", "label": "check"})
        except Exception:
            pass
    return cmds


def run_command(cmd: str, root: str, timeout: int = 25) -> bool:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, cwd=root)
        return r.returncode == 0
    except Exception:
        return False
# ---- end mirror ------------------------------------------------------------


def _mode():
    try:
        return json.loads(CONFIG.read_text()).get("streaming_gates", "shadow")
    except Exception:
        return "shadow"


def _sess_dir(session):
    d = CACHE_ROOT / session
    d.mkdir(parents=True, exist_ok=True)
    return d


def _content_hash(path):
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "missing"


def _load_edits(d):
    f = d / "edits.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return {}
    return {}


def _root_id(root):
    return hashlib.sha256(root.encode()).hexdigest()[:12]


def _signature(edits, root):
    items = sorted(f"{p}:{m['hash']}" for p, m in edits.items() if m["root"] == root)
    return hashlib.sha256("\n".join(items).encode()).hexdigest()[:12]


def record_edit(session, path):
    if _mode() == "off":
        return
    path = os.path.abspath(path)
    if os.path.splitext(path)[1].lower() not in CODE_EXTENSIONS:
        return  # match completion_gate: only code edits matter
    d = _sess_dir(session)
    root = find_project_root(os.path.dirname(path))
    edits = _load_edits(d)
    edits[path] = {"root": root, "hash": _content_hash(path), "ts": time.time()}
    (d / "edits.json").write_text(json.dumps(edits))

    sig = _signature(edits, root)
    rid = _root_id(root)
    done = d / f"v_{rid}_{sig}.done"
    run = d / f"v_{rid}_{sig}.run"
    if done.exists() or run.exists():
        return  # this exact tree state already verified / verifying

    # supersede: at most one in-flight verify per root (kill older signatures)
    for r in d.glob(f"v_{rid}_*.run"):
        try:
            pid = int(json.loads(r.read_text()).get("pid", 0))
            if pid:
                os.kill(pid, 15)
        except Exception:
            pass
        try:
            r.unlink()
        except Exception:
            pass

    p = subprocess.Popen(
        [sys.executable, str(RUNNER), "--root", root, "--done", str(done), "--run", str(run)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    run.write_text(json.dumps({"pid": p.pid, "root": root, "sig": sig, "started": time.time()}))


def recorded_files(session):
    return sorted(_load_edits(_sess_dir(session)).keys())


def evaluate(session, edited_files=None):
    mode = _mode()
    if mode == "off":
        return None
    d = _sess_dir(session)
    edits = _load_edits(d)
    roots = sorted({m["root"] for m in edits.values()})
    per_root = {}
    for root in roots:
        sig = _signature(edits, root)
        rid = _root_id(root)
        done = d / f"v_{rid}_{sig}.done"
        run = d / f"v_{rid}_{sig}.run"
        if done.exists():
            per_root[root] = done.read_text().strip()   # PASS | FAIL | NO_COMMANDS
        elif run.exists():
            per_root[root] = "PENDING"
        else:
            per_root[root] = "MISS"
    vals = list(per_root.values())
    if any(v == "FAIL" for v in vals):
        decision = "FAIL"
    elif any(v in ("PENDING", "MISS") for v in vals):
        decision = "INCONCLUSIVE"
    elif vals:
        decision = "PASS"                                # all PASS or NO_COMMANDS
    else:
        decision = "INCONCLUSIVE"
    return {"mode": mode, "authoritative": mode == "on",
            "decision": decision, "verdicts": per_root}


# ---- self-test (deterministic, Makefile-based, no language deps) -----------
def _selftest():
    import tempfile
    sess = "selftest-proj"
    d = _sess_dir(sess)
    for f in d.glob("*"):
        f.unlink()

    def mkproj(test_body):
        proj = Path(tempfile.mkdtemp())
        (proj / "Makefile").write_text(f"# proj\ntest:\n\t@{test_body}\n")
        src = proj / "mod.py"; src.write_text("x = 1\n")
        return proj, str(src)

    # passing project
    proj_ok, src_ok = mkproj("true")
    record_edit(sess, src_ok)
    # failing project
    proj_bad, src_bad = mkproj("exit 1")
    record_edit(sess, src_bad)

    for _ in range(80):
        r = evaluate(sess)
        if "PENDING" not in r["verdicts"].values() and "MISS" not in r["verdicts"].values():
            break
        time.sleep(0.1)

    res = evaluate(sess)
    assert res["authoritative"] is False, "shadow must not be authoritative"
    assert res["decision"] == "FAIL", f"expected FAIL (make test exit 1), got {res}"

    # re-edit the failing project's file to a new tree state -> MISS until reverified
    Path(src_bad).write_text("x = 2\n")
    record_edit(sess, src_bad)
    interim = evaluate(sess)
    assert interim["decision"] in ("INCONCLUSIVE", "FAIL"), f"stale must not flip to PASS: {interim}"

    print("stream_gates self-test PASS: real project commands (make test); "
          "FAIL detected; shadow non-authoritative; stale tree not reused")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(_selftest())
    print(json.dumps({"mode": _mode()}))
