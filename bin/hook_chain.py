#!/usr/bin/env python3
"""In-process hook chain runner — replaces N python spawns per event with 1.

Each registered chain runs its member scripts sequentially in this
interpreter via runpy, feeding every member the same stdin payload and
capturing each member's stdout envelope. Outputs are merged:

  - Stop-shaped chains: the first {"decision": "block"} envelope wins; any
    advisory stopReason strings are appended to its reason. With no block,
    advisory stopReasons are joined into one {"stopReason": ...}.
  - PostToolUse-shaped chains: hookSpecificOutput.additionalContext strings
    are joined into a single envelope.

Member stderr passes through untouched (advisory surface). A member that
exits 2 (blocking error) makes the chain exit 2 after all members have run,
preserving each member's chance to record telemetry.

Members keep their own should_run() profile gating — consolidation changes
the process model, not the policy. Deterministic ordering is a feature: the
gate members run after telemetry members, so checkpoints fire even when a
gate blocks (stop_gate.py's documented intent, previously best-effort).

Usage: hook_chain.py --chain {stop|post-edit|post-bash|post-failure}
"""

from __future__ import annotations

import argparse
import io
import json
import os
import runpy
import sys
import traceback

BIN = os.path.dirname(os.path.abspath(__file__))

# Chain membership mirrors the pre-consolidation settings.json registrations
# (2026-06-09). omni-mem save and dandori shadow hooks intentionally stay as
# separate registrations: the former is an external repo's bash hook, the
# latter is a live shadow experiment.
CHAINS = {
    "stop": [
        ("product_truth_auto_dispatch.py", []),
        ("completion_gate.py", ["--event", "stop"]),
        ("stop_reason_telemetry.py", []),
        ("replan_evidence_check.py", ["--strict"]),
        ("self_merge_check.py", []),
        ("stop_gate.py", []),
    ],
    "post-edit": [
        ("edit_verify_async.py", []),
        ("agent_def_edit_warn.py", []),
        ("case_recorder.py", []),
    ],
    "post-bash": [
        ("compaction_suggester.py", []),
        ("case_recorder.py", []),
        ("secret_leak_warn.py", []),
    ],
    "post-failure": [
        ("tool_failure_context.py", []),
        ("post_tool_use_failure_fal.py", []),
        ("case_recorder.py", ["--failure"]),
    ],
}


def run_member(script: str, args: list[str], raw_stdin: str) -> tuple[int, str]:
    """Execute one member script in-process. Returns (exit_code, stdout)."""
    path = os.path.join(BIN, script)
    old_stdin, old_stdout, old_argv = sys.stdin, sys.stdout, sys.argv
    buf = io.StringIO()
    sys.stdin = io.StringIO(raw_stdin)
    sys.stdout = buf
    sys.argv = [path] + args
    code = 0
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
    except Exception:
        sys.stderr.write(f"[hook_chain] {script} crashed:\n{traceback.format_exc()}")
        code = 1
    finally:
        sys.stdin, sys.stdout, sys.argv = old_stdin, old_stdout, old_argv
    return code, buf.getvalue()


def parse_envelope(stdout: str) -> dict | None:
    text = stdout.strip()
    if not text:
        return None
    # Members print exactly one JSON object; tolerate trailing noise by
    # taking the last line that parses.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def merge_and_emit(envelopes: list[dict]) -> None:
    block = next((e for e in envelopes if e.get("decision") == "block"), None)
    stop_reasons = [e["stopReason"] for e in envelopes
                    if isinstance(e.get("stopReason"), str) and e["stopReason"].strip()]
    contexts = []
    hook_event = None
    for e in envelopes:
        hso = e.get("hookSpecificOutput")
        if isinstance(hso, dict):
            ctx = hso.get("additionalContext")
            if isinstance(ctx, str) and ctx.strip():
                contexts.append(ctx)
                hook_event = hook_event or hso.get("hookEventName")

    if block is not None:
        if stop_reasons:
            block = dict(block)
            block["reason"] = "\n\n".join([block.get("reason", "")] + stop_reasons).strip()
        print(json.dumps(block))
        return
    out: dict = {}
    if stop_reasons:
        out["stopReason"] = "\n\n".join(stop_reasons)
    if contexts:
        out["hookSpecificOutput"] = {
            "hookEventName": hook_event or "PostToolUse",
            "additionalContext": "\n\n".join(contexts),
        }
    if out:
        print(json.dumps(out))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chain", choices=sorted(CHAINS), required=True)
    args = parser.parse_args()

    try:
        raw = sys.stdin.read()
    except IOError:
        raw = ""

    envelopes: list[dict] = []
    worst = 0
    for script, member_args in CHAINS[args.chain]:
        code, stdout = run_member(script, member_args, raw)
        if code == 2:
            worst = 2
        env = parse_envelope(stdout)
        if env:
            envelopes.append(env)

    merge_and_emit(envelopes)
    return worst


if __name__ == "__main__":
    sys.exit(main())
