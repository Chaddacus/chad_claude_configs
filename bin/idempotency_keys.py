#!/usr/bin/env python3
"""Portable idempotency key store for external side effects.

Closes CR-INV-002-EXTERNAL-SEND-IDEMPOTENT
(see ~/.claude/standards/CHAD_RUNTIME_INVARIANTS.md).

Append-only JSONL cache at ~/.claude/state/idempotency.jsonl. Callers
(chad-agent zoom_client, hooks, scripts) gate any external side effect on
`claim(op, key, window_seconds)` returning True. If the same (op, key) was
claimed within the window, the call returns False and the caller must skip
the side effect.

CLI:
    # Claim a new key (exit 0 if first claim, exit 10 if duplicate within window)
    idempotency_keys.py claim --op zoom_dm --key "chad@example.com:abc123..." --window 300

    # Check without claiming (exit 0 if would-claim, exit 10 if duplicate)
    idempotency_keys.py check --op zoom_dm --key "chad@example.com:abc123..." --window 300

    # Compute a content hash for use as the key suffix
    idempotency_keys.py hash --text "the message body"

    # Inspect recent claims for an op
    idempotency_keys.py recent --op zoom_dm --limit 20

Python:
    from idempotency_keys import claim, content_hash
    key = f"{recipient}:{content_hash(text)}"
    if not claim("zoom_dm", key, window_seconds=300):
        return  # duplicate — skip the send
    zoom.send_dm(recipient, text)

Design notes:
- JSONL append-only so concurrent writers don't corrupt; readers scan tail.
- Window is per-claim, so different callers can use different windows for the
  same op. Caller's window is the authoritative one for the claim event.
- Rotation: file is truncated when it exceeds MAX_LINES, keeping the most
  recent half. Triggered opportunistically on claim, not on a schedule.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

STATE_PATH = Path(
    os.environ.get(
        "CLAUDE_IDEMPOTENCY_PATH",
        os.path.expanduser("~/.claude/state/idempotency.jsonl"),
    )
)
MAX_LINES = 10000  # rotate when exceeded
EXIT_CLAIMED = 0
EXIT_DUPLICATE = 10
EXIT_USAGE = 2


def _ensure_parent() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)


def _read_recent(op: str, key: str, window_seconds: int, now: float) -> dict | None:
    """Return the most recent matching claim within the window, or None."""
    if not STATE_PATH.exists():
        return None
    cutoff = now - window_seconds
    # Tail scan — read whole file but only return the latest match.
    # File is bounded by MAX_LINES rotation so this is O(MAX_LINES) worst case.
    latest = None
    with STATE_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("op") != op or row.get("key") != key:
                continue
            ts = row.get("ts", 0)
            if ts < cutoff:
                continue
            if latest is None or ts > latest.get("ts", 0):
                latest = row
    return latest


def _rotate_if_needed() -> None:
    """If the file has more than MAX_LINES rows, keep the most recent half."""
    if not STATE_PATH.exists():
        return
    try:
        with STATE_PATH.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    if len(lines) <= MAX_LINES:
        return
    keep = lines[-(MAX_LINES // 2):]
    tmp = STATE_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.writelines(keep)
    tmp.replace(STATE_PATH)


def content_hash(text: str) -> str:
    """SHA-256 hex digest of `text`. Use as the content portion of an idempotency key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check(op: str, key: str, window_seconds: int) -> dict | None:
    """Return the recent matching claim within the window, or None.

    Read-only; does not write a claim."""
    _ensure_parent()
    return _read_recent(op, key, window_seconds, time.time())


def claim(op: str, key: str, window_seconds: int, **extra) -> bool:
    """Atomically claim (op, key) within window_seconds.

    Returns True if this is a new claim (caller should proceed with side effect).
    Returns False if a prior claim within the window exists (caller must skip).

    Note: this is not atomic across multiple processes — JSONL append is
    near-atomic on POSIX for small writes, but two simultaneous claims for the
    same key could both succeed. For chad-agent's send rate this is acceptable;
    if you need stricter atomicity, wrap with fcntl.flock on STATE_PATH.
    """
    _ensure_parent()
    now = time.time()
    prior = _read_recent(op, key, window_seconds, now)
    if prior is not None:
        return False
    row = {
        "ts": now,
        "op": op,
        "key": key,
        "window_seconds": window_seconds,
    }
    if extra:
        row["extra"] = extra
    with STATE_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    _rotate_if_needed()
    return True


def recent(op: str, limit: int = 20) -> list[dict]:
    """Return the most recent `limit` claims for `op`, newest first."""
    if not STATE_PATH.exists():
        return []
    matches: list[dict] = []
    with STATE_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("op") == op:
                matches.append(row)
    return list(reversed(matches[-limit:]))


def _cmd_claim(args: argparse.Namespace) -> int:
    ok = claim(args.op, args.key, args.window)
    if args.verbose:
        msg = "claimed" if ok else "duplicate"
        print(json.dumps({"op": args.op, "key": args.key, "result": msg}))
    return EXIT_CLAIMED if ok else EXIT_DUPLICATE


def _cmd_check(args: argparse.Namespace) -> int:
    prior = check(args.op, args.key, args.window)
    if args.verbose:
        if prior is None:
            print(json.dumps({"op": args.op, "key": args.key, "result": "would_claim"}))
        else:
            print(json.dumps({"op": args.op, "key": args.key, "result": "duplicate", "prior": prior}))
    return EXIT_CLAIMED if prior is None else EXIT_DUPLICATE


def _cmd_hash(args: argparse.Namespace) -> int:
    print(content_hash(args.text))
    return EXIT_CLAIMED


def _cmd_recent(args: argparse.Namespace) -> int:
    rows = recent(args.op, args.limit)
    print(json.dumps(rows, indent=2, default=str))
    return EXIT_CLAIMED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Portable idempotency key store.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_claim = sub.add_parser("claim", help="Claim a key; exit 0 if new, exit 10 if duplicate within window.")
    p_claim.add_argument("--op", required=True, help="Operation name, e.g. zoom_dm, slack_post, email_send.")
    p_claim.add_argument("--key", required=True, help="Idempotency key, typically recipient:content_hash.")
    p_claim.add_argument("--window", type=int, default=300, help="Dedup window in seconds (default 300 = 5 min).")
    p_claim.add_argument("--verbose", action="store_true", help="Print JSON result to stdout.")
    p_claim.set_defaults(func=_cmd_claim)

    p_check = sub.add_parser("check", help="Check without claiming.")
    p_check.add_argument("--op", required=True)
    p_check.add_argument("--key", required=True)
    p_check.add_argument("--window", type=int, default=300)
    p_check.add_argument("--verbose", action="store_true")
    p_check.set_defaults(func=_cmd_check)

    p_hash = sub.add_parser("hash", help="Compute SHA-256 hex digest of text (for use as the key content portion).")
    p_hash.add_argument("--text", required=True)
    p_hash.set_defaults(func=_cmd_hash)

    p_recent = sub.add_parser("recent", help="List the most recent claims for an op.")
    p_recent.add_argument("--op", required=True)
    p_recent.add_argument("--limit", type=int, default=20)
    p_recent.set_defaults(func=_cmd_recent)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
