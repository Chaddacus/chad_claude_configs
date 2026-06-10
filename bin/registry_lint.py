#!/usr/bin/env python3
"""Schema lint for ~/.claude/policy/phase_questions.yaml (Slice 2).

Usage:
  python3 ~/.claude/bin/registry_lint.py [path/to/phase_questions.yaml]

Exit 0 = clean. Exit 1 = lint failures (printed to stderr).

Schema (mirrors the inline registry shape in auto_runtime_common.py):
  registry_version: str (required, non-empty)
  phases: dict[phase_name → {questions: list[question]}]
    phase_name must be in PHASE_ENUM
  loop_invariant:
    triggers: dict (free-form; warns if unknown keys)
    max_invariant_tokens: int (>0)
    questions: list[question]
  question:
    id: str (unique across the registry)
    question: str (non-empty)
    targets_decision_kind: str ∈ OBSERVABLE_DECISION_KINDS
    any_evidence_required: list[str] (non-empty)
    skip_when: optional dict, recognised keys: route_in (list of route IDs)
"""

from __future__ import annotations

import sys
from pathlib import Path

# Import OBSERVABLE_DECISION_KINDS + PHASE_ENUM from runtime; lint must agree
# with the live constants.
sys.path.insert(0, str(Path.home() / ".claude" / "bin"))
import auto_runtime_common as rt  # noqa: E402

VALID_ROUTES = ("R1", "R2", "R3", "R4", "R5")
KNOWN_TRIGGER_KEYS = {"event_count_since_last", "on_route_promotion"}
KNOWN_SKIP_WHEN_KEYS = {"route_in"}
DEFAULT_REGISTRY = (
    Path.home() / ".claude" / "policy" / "phase_questions.yaml"
)


def _err(errs: list[str], where: str, msg: str) -> None:
    errs.append(f"{where}: {msg}")


def _validate_question(
    q, where: str, seen_ids: set[str], errs: list[str]
) -> None:
    if not isinstance(q, dict):
        _err(errs, where, "question must be a mapping")
        return
    qid = q.get("id")
    if not isinstance(qid, str) or not qid.strip():
        _err(errs, where, "id missing or empty")
    elif qid in seen_ids:
        _err(errs, where, f"duplicate question id: {qid}")
    else:
        seen_ids.add(qid)
    text = q.get("question")
    if not isinstance(text, str) or not text.strip():
        _err(errs, where, "question text missing or empty")
    tdk = q.get("targets_decision_kind")
    if tdk not in rt.OBSERVABLE_DECISION_KINDS:
        _err(
            errs, where,
            f"targets_decision_kind={tdk!r} not in OBSERVABLE_DECISION_KINDS "
            f"{rt.OBSERVABLE_DECISION_KINDS}",
        )
    aer = q.get("any_evidence_required")
    if not isinstance(aer, list) or not aer:
        _err(errs, where, "any_evidence_required must be a non-empty list")
    elif not all(isinstance(x, str) and x for x in aer):
        _err(errs, where, "any_evidence_required entries must be non-empty strings")
    skip_when = q.get("skip_when")
    if skip_when is not None:
        if not isinstance(skip_when, dict):
            _err(errs, where, "skip_when must be a mapping")
        else:
            for k in skip_when.keys():
                if k not in KNOWN_SKIP_WHEN_KEYS:
                    _err(errs, where, f"skip_when has unknown key: {k}")
            route_in = skip_when.get("route_in")
            if route_in is not None:
                if not isinstance(route_in, list):
                    _err(errs, where, "skip_when.route_in must be a list")
                else:
                    for r in route_in:
                        if r not in VALID_ROUTES:
                            _err(
                                errs, where,
                                f"skip_when.route_in contains invalid route: {r}",
                            )


def lint_registry(data) -> list[str]:
    """Return a list of error strings. Empty list = clean."""
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["root: registry must be a mapping"]
    rv = data.get("registry_version")
    if not isinstance(rv, str) or not rv.strip():
        _err(errs, "root", "registry_version missing or empty")
    seen_ids: set[str] = set()
    phases = data.get("phases", {})
    if not isinstance(phases, dict):
        _err(errs, "phases", "must be a mapping")
    else:
        for pname, pblock in phases.items():
            where = f"phases.{pname}"
            if pname not in rt.PHASE_ENUM:
                _err(errs, where, f"unknown phase name (PHASE_ENUM={rt.PHASE_ENUM})")
            if not isinstance(pblock, dict):
                _err(errs, where, "phase block must be a mapping")
                continue
            qs = pblock.get("questions", [])
            if not isinstance(qs, list):
                _err(errs, f"{where}.questions", "must be a list")
                continue
            for i, q in enumerate(qs):
                _validate_question(q, f"{where}.questions[{i}]", seen_ids, errs)
    inv = data.get("loop_invariant", {})
    if not isinstance(inv, dict):
        _err(errs, "loop_invariant", "must be a mapping")
    else:
        trig = inv.get("triggers", {})
        if not isinstance(trig, dict):
            _err(errs, "loop_invariant.triggers", "must be a mapping")
        else:
            for k in trig.keys():
                if k not in KNOWN_TRIGGER_KEYS:
                    _err(errs, "loop_invariant.triggers", f"unknown key: {k}")
        max_tok = inv.get("max_invariant_tokens")
        if not isinstance(max_tok, int) or max_tok <= 0:
            _err(errs, "loop_invariant.max_invariant_tokens", "must be int > 0")
        qs = inv.get("questions", [])
        if not isinstance(qs, list):
            _err(errs, "loop_invariant.questions", "must be a list")
        else:
            for i, q in enumerate(qs):
                _validate_question(
                    q, f"loop_invariant.questions[{i}]", seen_ids, errs,
                )
    return errs


def _load_yaml(path: Path):
    try:
        import yaml  # type: ignore
    except ImportError:
        print("pyyaml not installed", file=sys.stderr)
        sys.exit(2)
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_REGISTRY
    if not path.exists():
        print(f"registry file not found: {path}", file=sys.stderr)
        return 1
    data = _load_yaml(path)
    errs = lint_registry(data)
    if errs:
        for e in errs:
            print(f"registry_lint: {e}", file=sys.stderr)
        return 1
    print(f"registry_lint: ok ({path})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
