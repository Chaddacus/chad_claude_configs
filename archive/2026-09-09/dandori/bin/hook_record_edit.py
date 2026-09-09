#!/usr/bin/env python3
"""PostToolUse(Edit|Write) adapter: feed the streaming gate off the critical path.

Reads the hook payload from stdin, extracts session_id + file_path, launches a
background verify via stream_gates.record_edit. ALWAYS exits 0 and never raises —
it must not block or wedge the tool-use chain.
"""
import json
import sys

sys.path.insert(0, "/Users/chadsimon/.claude/dandori/bin")


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        import stream_gates
        session = str(payload.get("session_id") or "default")
        ti = payload.get("tool_input") or {}
        path = ti.get("file_path") or ti.get("path") or payload.get("file_path")
        if path:
            stream_gates.record_edit(session, path)
    except Exception:
        pass  # shadow mode: never interfere with the live run
    return 0


if __name__ == "__main__":
    sys.exit(main())
