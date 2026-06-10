#!/usr/bin/env python3
"""Background project verifier for the streaming gate.

Runs the project's resolved commands (same set completion_gate would run) and
writes the verdict to --done, then removes --run. Invoked detached by
stream_gates.record_edit. Never raises into the caller.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, "/Users/chadsimon/.claude/dandori/bin")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--done", required=True)
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    verdict = "NO_COMMANDS"
    try:
        import stream_gates
        cmds = stream_gates.resolve_commands(a.root)
        if cmds:
            verdict = "PASS"
            for c in cmds:
                if not stream_gates.run_command(c["cmd"], a.root):
                    verdict = "FAIL"
                    break
    except Exception:
        verdict = "ERROR"
    try:
        Path(a.done).write_text(verdict)
    except Exception:
        pass
    try:
        os.unlink(a.run)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
