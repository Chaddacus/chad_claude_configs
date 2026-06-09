#!/usr/bin/env python3
"""Track loop progression quality for plan/implementation gates."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import file_lock, now_iso, resolve_artifacts_root, sanitize_token


@dataclass
class ProgressSnapshot:
    loop: int = 0
    best_score: int = 0
    last_score: int = 0
    stagnant_loops: int = 0


def _empty_state(ttl_hours: int) -> dict:
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    return {
        "schema_version": "progress.v1",
        "updated_at": now_iso(),
        "expires_at": expires_at,
        "plan": asdict(ProgressSnapshot()),
        "impl": asdict(ProgressSnapshot()),
    }


def _is_expired(state: dict) -> bool:
    raw = str(state.get("expires_at", "")).strip()
    if not raw:
        return False
    try:
        expires = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > expires


def _load_state(state_path: Path, ttl_hours: int) -> dict:
    if not state_path.exists():
        return _empty_state(ttl_hours)
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_state(ttl_hours)
    if not isinstance(payload, dict):
        return _empty_state(ttl_hours)
    if payload.get("schema_version") != "progress.v1" or _is_expired(payload):
        return _empty_state(ttl_hours)
    return payload


def _save_state(state_path: Path, state: dict, ttl_hours: int) -> None:
    state["updated_at"] = now_iso()
    state["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def update_progress(
    *,
    artifacts_root: Path,
    track_id: str,
    kind: str,
    score: int,
    passed_100: bool,
    stall_limit: int = 2,
    ttl_hours: int = 24,
) -> dict:
    if kind not in {"plan", "impl"}:
        raise ValueError("kind must be 'plan' or 'impl'")

    token = sanitize_token(track_id)
    track_dir = artifacts_root / token
    state_path = track_dir / "state.json"
    lock_path = track_dir / "state.lock"

    with file_lock(lock_path):
        state = _load_state(state_path, ttl_hours)
        snapshot = dict(state.get(kind, {}))
        current = ProgressSnapshot(
            loop=int(snapshot.get("loop", 0) or 0),
            best_score=int(snapshot.get("best_score", 0) or 0),
            last_score=int(snapshot.get("last_score", 0) or 0),
            stagnant_loops=int(snapshot.get("stagnant_loops", 0) or 0),
        )

        regressed = False
        stalled = False
        previous = current.last_score

        current.loop += 1
        if current.loop > 1:
            if score < previous:
                regressed = True
            elif score == previous and not passed_100:
                current.stagnant_loops += 1
                stalled = current.stagnant_loops >= stall_limit
            else:
                current.stagnant_loops = 0
        else:
            current.stagnant_loops = 0

        current.last_score = score
        if score > current.best_score:
            current.best_score = score

        state[kind] = asdict(current)
        _save_state(state_path, state, ttl_hours)

    return {
        "track_id": token,
        "kind": kind,
        "loop": current.loop,
        "best_score": current.best_score,
        "last_score": current.last_score,
        "previous_score": previous,
        "stagnant_loops": current.stagnant_loops,
        "regressed": regressed,
        "stalled": stalled,
        "stall_limit": stall_limit,
        "state_path": str(state_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Update planning-gate progression score.")
    parser.add_argument("--track-id", required=True)
    parser.add_argument("--kind", required=True, choices=["plan", "impl"])
    parser.add_argument("--score", type=int, required=True)
    parser.add_argument("--passed-100", action="store_true", help="Set when 100% smoke gate is pass")
    parser.add_argument("--stall-limit", type=int, default=2)
    parser.add_argument("--ttl-hours", type=int, default=24)
    parser.add_argument("--artifacts-root", default=None)
    args = parser.parse_args()

    artifacts_root = resolve_artifacts_root(args.artifacts_root)
    summary = update_progress(
        artifacts_root=artifacts_root,
        track_id=args.track_id,
        kind=args.kind,
        score=args.score,
        passed_100=args.passed_100,
        stall_limit=args.stall_limit,
        ttl_hours=args.ttl_hours,
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
