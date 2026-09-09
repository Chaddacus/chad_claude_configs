#!/usr/bin/env python3
"""Emit canonical objective intent artifacts for governed runtime entry."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from common import (
    OBJECTIVE_SHAPE_STATUS_VALUES,
    PLAN_INTENT_SCHEMA_VERSION,
    build_objective_intent_payload,
    ensure_python_3_11,
    load_json_file,
    plan_artifact_paths,
    resolve_artifacts_root,
    sanitize_token,
    stable_review,
    write_json_file,
    write_objective_intent_artifact,
    write_plan_frontloaded_artifacts,
)


def _load_clarification_payload(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    candidate = Path(raw)
    text = candidate.read_text(encoding="utf-8") if candidate.exists() else raw
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("clarification_json_must_decode_to_object")
    return payload


def _load_request_payload(args: argparse.Namespace) -> tuple[dict[str, Any] | None, str | None]:
    if args.request_json:
        payload = json.loads(args.request_json)
        if isinstance(payload, dict):
            return payload, None
        if isinstance(payload, str):
            return None, payload
        raise ValueError("request_json_must_be_object_or_string")
    if args.request_stdin:
        return None, sys.stdin.read()
    if args.request_file:
        raw = Path(args.request_file).read_text(encoding="utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None, raw
        if isinstance(payload, dict):
            return payload, None
        if isinstance(payload, str):
            return None, payload
        raise ValueError("request_file_must_decode_to_object_or_string")
    return None, None


def _merge_clarification(
    request_payload: dict[str, Any] | None,
    clarification_payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if request_payload is None and clarification_payload is None:
        return None
    merged = dict(request_payload or {})
    if clarification_payload:
        for key, value in clarification_payload.items():
            if isinstance(value, list) and isinstance(merged.get(key), list):
                merged[key] = [*merged[key], *value]
            elif isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    return merged


def _resolve_real_bin() -> str | None:
    candidates = [
        os.environ.get("CODEX_INTENT_MODEL_BIN"),
        os.environ.get("CODEX_REAL_BIN"),
        shutil.which("codex.real"),
        str(Path.home() / ".local" / "bin" / "codex.real"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().exists():
            return str(Path(candidate).expanduser())
    return None


def _extract_json_object(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    for start in range(len(text)):
        if text[start] != "{":
            continue
        snippet = text[start:]
        try:
            payload = json.loads(snippet)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("intent_model_output_missing_json_object")


def _normalize_request_via_model(
    *,
    request_text: str,
    clarification_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    real_bin = _resolve_real_bin()
    if not real_bin:
        raise RuntimeError("intent_model_binary_unavailable")
    clarification_text = json.dumps(clarification_payload, indent=2, sort_keys=True) if clarification_payload else "{}"
    prompt = (
        "Return JSON only.\n"
        "Normalize the request into an objective-intent object with keys: "
        "objective, success_criteria, audience, scope_boundaries, authority_sensitive_decisions, "
        "known_unknowns, discoverable_unknowns, discoverable_resolution_log, non_goals, clarification_questions, "
        "clarification_batch_count, objective_shape_status, normalization_source.\n"
        "Rules: resolve discoverable ambiguity yourself; ask clarification questions only for product meaning, "
        "authority/security boundaries, or missing non-discoverable success criteria; successful raw-request rewrites "
        "default to objective_shape_status=accepted_rewritten; use blocked only for authority/security blockers.\n"
        f"Raw request:\n{request_text.strip()}\n"
        f"Clarification answers:\n{clarification_text}\n"
    )
    completed = subprocess.run(
        [real_bin, "exec", prompt],
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"intent_model_exec_failed:{completed.returncode}:{completed.stderr.strip()}")
    payload = _extract_json_object(completed.stdout)
    payload["raw_request"] = request_text.strip()
    payload["normalization_source"] = str(payload.get("normalization_source") or "model_raw_request").strip()
    if not str(payload.get("objective_shape_status", "")).strip():
        payload["objective_shape_status"] = "accepted_rewritten"
    return payload


def compile_intent_payload(
    *,
    track_id: str,
    artifacts_root,
    plan_payload: dict[str, Any] | None = None,
    request_payload: dict[str, Any] | None = None,
    request_text: str | None = None,
) -> dict:
    track_token = sanitize_token(track_id)
    objective_intent, intent_path = write_objective_intent_artifact(
        track_id=track_id,
        artifacts_root=artifacts_root,
        plan=plan_payload,
        request_payload=request_payload,
        request_text=request_text,
    )
    written: dict[str, str] = {"objective_intent": str(intent_path)}
    if plan_payload is not None:
        written.update(
            write_plan_frontloaded_artifacts(
                plan_payload,
                track_id=track_id,
                artifacts_root=artifacts_root,
                include=("intent", "readiness"),
            )
        )
    else:
        intent_artifact = {
            "schema_version": PLAN_INTENT_SCHEMA_VERSION,
            "track_id": track_id,
            "objective_ref": track_token,
            "objective_intent_ref": "objective.intent.json",
            "intent_contract": objective_intent.get("intent_contract"),
            "clarification_governor": objective_intent.get("clarification_governor"),
            "autonomy_level": "",
        }
        write_json_file(plan_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["intent"], intent_artifact)
        written["intent"] = str(plan_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["intent"])

    shape_status = str(objective_intent.get("objective_shape_status", "")).strip()
    review_status = "approve"
    content = "Canonical objective intent artifact emitted."
    next_step = "Run initialize_session.py using objective.intent.json, then compile_plan.py."
    if objective_intent.get("clarification_needed") is True and shape_status != "blocked":
        review_status = "revise"
        content = "Clarification governor requires one frontloaded clarification batch before planning."
        next_step = "Resolve clarification_reasons into objective.intent.json, then rerun compile_intent.py or continue with compile_plan.py when approved."
    if shape_status == "revise_required":
        review_status = "revise"
        content = "Intent compiler requires objective-shape revision before execution."
        next_step = "Rewrite the objective shape, then rerun compile_intent.py."
    elif shape_status == "blocked":
        review_status = "blocked"
        content = "Intent compiler blocked the objective shape."
        next_step = "Resolve the authority or security blocker, then rerun compile_intent.py."
    elif shape_status not in OBJECTIVE_SHAPE_STATUS_VALUES:
        review_status = "blocked"
        content = "Intent compiler emitted an invalid objective-shape status."
        next_step = "Fix the request normalization inputs and rerun compile_intent.py."

    return stable_review(
        gate="intent-compiler",
        status=review_status,
        content=content,
        next_step=next_step,
        meta={
            "track_id": track_id,
            "track_token": track_token,
            "objective_shape_status": shape_status,
            "clarification_needed": objective_intent.get("clarification_needed") is True,
            "clarification_reasons": objective_intent.get("clarification_reasons", []),
            "artifacts": written,
        },
    )


def main() -> int:
    ensure_python_3_11()

    parser = argparse.ArgumentParser(description="Emit canonical objective intent artifacts.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan-json")
    source.add_argument("--request-json")
    source.add_argument("--request-file")
    source.add_argument("--request-stdin", action="store_true")
    parser.add_argument("--clarification-json", default=None)
    parser.add_argument("--review-json-out", required=True)
    parser.add_argument("--track-id", required=True)
    parser.add_argument("--artifacts-root", default=None)
    args = parser.parse_args()

    artifacts_root = resolve_artifacts_root(args.artifacts_root)

    try:
        plan_payload = load_json_file(args.plan_json) if args.plan_json else None
        clarification_payload = _load_clarification_payload(args.clarification_json)
        request_payload, request_text = _load_request_payload(args)
        request_payload = _merge_clarification(request_payload, clarification_payload)
        if plan_payload is None and request_text is not None:
            request_payload = _normalize_request_via_model(
                request_text=request_text,
                clarification_payload=clarification_payload,
            )
            if clarification_payload:
                request_payload["clarification_batch_count"] = 1
                request_payload.setdefault("clarification_questions", [])
            request_text = None
        review = compile_intent_payload(
            plan_payload=plan_payload,
            request_payload=request_payload,
            request_text=request_text,
            track_id=args.track_id,
            artifacts_root=artifacts_root,
        )
    except Exception as exc:
        review = stable_review(
            gate="intent-compiler",
            status="blocked",
            content="Intent compiler failed.",
            blocked_fields=[str(exc)],
            next_step="Fix the intent inputs and rerun compile_intent.py.",
        )
        write_json_file(args.review_json_out, review)
        print(json.dumps(review, sort_keys=True))
        return 1

    write_json_file(args.review_json_out, review)
    print(json.dumps(review, sort_keys=True))
    return 0 if review["status"] == "approve" else 1


if __name__ == "__main__":
    raise SystemExit(main())
