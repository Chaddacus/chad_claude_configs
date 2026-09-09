# Phase loop corpus — fixture schema

Defines the JSONL fixture format consumed by ship gates (Slices 1a/1b/1c/3)
and the validation framework (Slice V `analyze.py`).

Each line of `phase_loop_corpus.jsonl` is one fixture object:

```json
{
  "task_id":           "r3-impl-1",
  "prompt":            "what the user actually says to the loop",
  "expected_route":    "R1 | R2 | R3 | R4 | R5",
  "expected_phase_path": ["discovery", "design", "build", "verify", "closeout"],
  "expected_decisions": [
    {
      "kind":                "scope | owned_files | validation_plan | next_action | phase | route | tool_invocation",
      "expected_changed":    true | false,
      "min_evidence_types":  ["repo_search", "file_read", ...],
      "expected_no_change_reason_regex": ".*"
    }
  ],
  "budget": {
    "max_cycles":        24,
    "max_tokens":        30000,
    "max_wall_clock_ms": 600000
  },
  "expected_acceptance_signature": {
    "acceptance_state": "accepted | rework | blocked | deferred",
    "additional_assertions": []
  },
  "baseline_event_log_path": null,
  "notes": "free-text human description"
}
```

## Field semantics

- `task_id`: unique kebab-case ID. Prefix indicates route: `r1-`, `r2-`, `r3-`, `r4-`, `r5-`, or `neg-` for negative controls.
- `prompt`: realistic input to `auto_runtime.py init --task "..."`.
- `expected_route`: what `classify_route()` should return. Tested in regression suite.
- `expected_phase_path`: ordered list of `phase_changed` event `to_phase` values expected to appear in the event log. Empty list means no phase machinery should fire (R1 bypass + R5-unresolved).
- `expected_decisions`: per-kind `decision_record` expectations. Used by Slice 1b ship gate. `expected_changed=true` means `before_state_hash != after_state_hash` per the canonical state payload. `expected_changed=false` requires `no_change_reason` to be present (matches `expected_no_change_reason_regex` if provided).

  **Observable decision kinds (Slice 1b scope):** `phase`, `route`, `next_action`, `owned_files`. The plan-final §3 schema also lists `scope`, `validation_plan`, and `tool_invocation` — these require state plumbing not present in `auto_runtime_common.py` and are deferred to a future slice. Fixtures only reference the 4 observable kinds. When state plumbing for the missing kinds lands, fixtures may be amended (APPEND new expected_decisions entries; existing entries stay frozen once baseline captured).
- `budget`: hard caps; track aborts if exceeded.
- `expected_acceptance_signature`: how to recognize success at closeout. Used by H-δ acceptance-rate measurement.
- `baseline_event_log_path`: filled in by `analyze.py baseline-capture` (Slice V). `null` at fixture authoring time.
- `notes`: optional human context.

## Stratification (15 fixtures total)

| Route | Count | Purpose |
|---|---|---|
| R1 | 2 | Lookup/factual — bypass machinery |
| R2 | 4 | Small implementation; minimal machinery |
| R3 | 3 | Non-trivial implementation; full machinery |
| R4 | 3 | Auth/security/migration/billing variants; full machinery + extra evidence |
| R5 | 1 | Ambiguity resolves to R3 mid-run |
| neg | 2 | Negative controls — registry question should produce changed=false |

## Validation

Fixtures must parse as JSON line-by-line. Until `analyze.py` (Slice V) lands,
verify with:
```bash
python3 -c "import json; [json.loads(l) for l in open('phase_loop_corpus.jsonl')]"
```

A formal schema lint will be added in Slice 2 (`registry_lint.py`).

## Amendment protocol

- New fixtures: APPEND to corpus. Existing fixtures are frozen once baseline
  is captured.
- Changing an existing fixture invalidates its baseline; must re-run
  `analyze.py baseline-capture` for that fixture.
- Removing fixtures requires a separate ADR documenting why (corpus is a
  binding artifact, like `hypotheses.yaml`).

## References

- Plan: `~/.codex-spar/stage-aware-orchestrator-loop/plan-final.md` §3, §7
- Hypotheses: `~/.claude/policy/hypotheses.yaml`
- Consumed by: `~/.claude/bench/analyze.py` (future, Slice V)
