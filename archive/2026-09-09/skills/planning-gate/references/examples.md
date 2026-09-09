# Planning Gate Examples

## 0) Contract boundary
`plan.v1` is the outer planning and compatibility contract. It is not directly executable by the kernel.

`execution-plan.v1` is compiled from `plan.v1` and is the canonical execution IR. The kernel executes only `execution-plan.v1`.

If `plan.v1` cannot compile into valid `execution-plan.v1`, execution must not start. If the outer plan and compiled IR disagree semantically, that is a compile defect, not a runtime repair path.

## 1) Validate plan
Plan JSON must include a concrete `definition_of_done` list covering:
- `correctness`
- `tests`
- `security`
- `observability`
- `rollback`
Each item must include string fields (`id`, `category`, `criterion`, `verification`) and `verification` must reference executable evidence (command/script/artifact).
Plan JSON must also include governed-control-plane fields such as:
- `non_goals`
- `quality_bar`
- `objective_closure_policy`
- `migration_fallback_policy`
- `scheduler_policy`
- `hardening_budget`
- `plan_gap_report`
- `plan_sufficiency_report`
- `objective_requirements`
- `objective_coverage_map`
- `assumptions_ledger`
- `integration_map`
- `requirement_risk_rank`
- `packets`
- `session_harness`

Packets must form a valid DAG and each packet must have one primary behavior plus explicit dependency mode and allowed scope.
Plans must also carry intent, readiness, and momentum data for autonomous completion:
- `intent_contract`
- `clarification_governor`
- `autonomous_session_readiness`
- `momentum_map`
- `frontier_map`
- `autonomy_level`

Compilation rule:
- the compiler is the only legal bridge from `plan.v1` to `execution-plan.v1`
- the compiler must emit fully kernel-valid IR or fail
- authored packet ids stay stable through compile
- generated validation packet ids may appear only through compiler-defined generated-validation rules

```bash
python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/compile_intent.py" \
  --plan-json /abs/path/plan.json \
  --review-json-out /abs/path/review.plan.intent.json \
  --track-id task-123

python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/initialize_session.py" \
  --plan-json /abs/path/plan.json \
  --review-json-out /abs/path/review.plan.init.json \
  --track-id task-123

python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/compile_plan.py" \
  --plan-json /abs/path/plan.json \
  --review-json-out /abs/path/review.plan.compile.json \
  --track-id task-123

python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/verify_plan.py" \
  --plan-json /abs/path/plan.json \
  --review-json-out /abs/path/review.plan.verify.json \
  --track-id task-123

python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/validate_plan.py" \
  --plan-json /abs/path/plan.json \
  --review-json-out /abs/path/review.plan.json \
  --track-id task-123
```

## 2) Capture command evidence
```bash
python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/run_cmd_capture.py" \
  --track-id task-123 \
  --stage 50% \
  --name unit-tests \
  --cwd /abs/path/repo \
  --timeout-sec 300 \
  -- pytest -q
```

Use the output `proof_artifact` + `proof_hash` in implementation JSON.

Implementation payloads must include adaptive-memory and prompt evidence arrays. Example:

```json
{
  "schema_version": "implementation.v1",
  "summary": "Adaptive memory rollout implemented",
  "changed_files": ["/abs/path/file.ts"],
  "tests_run": [],
  "smoke_results": [],
  "logging_evidence": [],
  "rollback_validation": {
    "executed": true,
    "result": "pass",
    "evidence": "Rollback script dry-run succeeded",
    "proof_artifact": "planning_artifacts/task-123/100%-rollback.manifest.json",
    "proof_hash": "<sha256>"
  },
  "memory_retrieval_evidence": [
    {
      "tool": "mcp__codex-mem__build_context",
      "query": "pref:frontend",
      "result_count": 4
    }
  ],
  "preferences_applied": [
    {
      "key": "pref:frontend.iteration_size",
      "decision": "Use one-region-per-iteration UI loop",
      "rationale": "Matches retrieved preference and reduces regression risk."
    }
  ],
  "skill_trigger_eval_results": [
    {
      "skill": "memory-adaptation",
      "false_positive_rate": 0.08,
      "false_negative_rate": 0.04,
      "threshold_passed": true
    }
  ],
  "prompt_contract_used": [
    {
      "name": "frontend-contract.v1",
      "required_context": "Figma selection URL + current route screenshot",
      "required_constraints": "Stack constraints, mobile requirement, behavior requirements",
      "verification_section": "Responsive + a11y + visual checks",
      "done_when": "Evidence artifacts generated and checks pass"
    }
  ],
  "frontend_roundtrip_evidence": [
    {
      "step": "figma-ingest",
      "evidence": "Selection URL captured and mapped to component ownership"
    }
  ],
  "objective_status": {
    "objective_id": "task-123",
    "closure_state": "OBJECTIVE_COMPLETE",
    "completed_packets": ["packet-a", "packet-b"],
    "pending_packets": [],
    "blocked_packets": [],
    "deferred_packets": [],
    "boundary_shrunk_remainder": [],
    "artifact_path": "planning_artifacts/task-123/objective.status.json"
  },
  "schedule_artifact": "planning_artifacts/task-123/objective.schedule.json",
  "momentum_artifact": "planning_artifacts/task-123/objective.momentum.json",
  "blockers_artifact": "planning_artifacts/task-123/objective.blockers.json",
  "packet_verdicts": [
    {
      "packet_id": "packet-a",
      "runtime_state": "accepted",
      "verifier_output": "accepted",
      "allowed_scope_status": "within_scope",
      "artifact_path": "planning_artifacts/task-123/packets/packet-a.verdict.json"
    }
  ],
  "checkpoint_commit": "checkpoint-task-123",
  "checkpoint_blocked": false,
  "checkpoint_block_reason": "",
  "checkpoint_block_evidence": "",
  "bootstrap_commands": ["npm test"],
  "validation_commands": ["python3.11 -m pytest"],
  "clean_state_assertions": ["Fresh session can resume from checkpoint artifacts without chat reconstruction"],
  "migration_fallback": {
    "used": false,
    "reason": "no migration defect observed",
    "artifact_path": ""
  }
}
```

## 3) Validate implementation evidence
```bash
python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/validate_impl.py" \
  --plan-json /abs/path/plan.json \
  --impl-json /abs/path/implementation.json \
  --review-json-out /abs/path/review.impl.json \
  --track-id task-123
```

## 4) Finalize
```bash
python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/finalize_gate.py" \
  --plan-json /abs/path/plan.json \
  --impl-json /abs/path/implementation.json \
  --review-json /abs/path/review.impl.json \
  --track-id task-123 \
  --out /abs/path/finalize.json
```

Approval rule: only proceed when `finalize.json` contains `"ok": true`.
The finalize payload also emits `objective_closure_state`, `accepted_type`, and `migration_fallback_used` for postflight compatibility.

## 5) Replay a successful governed run
```bash
python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/objective_runtime_status.py" \
  --track-id task-123 \
  --artifacts-root /abs/path/artifacts \
  --view summary

python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/objective_runtime_replay.py" \
  --track-id task-123 \
  --artifacts-root /abs/path/artifacts \
  --view timeline
```

Use `timeline` to confirm the kernel path from `ready` through `verifying` into terminal closure, with verification summaries attached to each step.

## 6) Replay an unsafe trap
```bash
python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/objective_runtime_replay.py" \
  --track-id task-123 \
  --artifacts-root /abs/path/artifacts \
  --view trap

python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/objective_runtime_replay.py" \
  --track-id task-123 \
  --artifacts-root /abs/path/artifacts \
  --view terminal
```

Use `trap` to identify the first invalid transition or invariant failure and `terminal` to explain why the kernel halted fail-closed.

## 7) Run went bad: minimal diagnosis flow
```bash
python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/objective_runtime_status.py" \
  --track-id task-123 \
  --artifacts-root /abs/path/artifacts \
  --view summary

python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/objective_runtime_replay.py" \
  --track-id task-123 \
  --artifacts-root /abs/path/artifacts \
  --view timeline

python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/objective_runtime_replay.py" \
  --track-id task-123 \
  --artifacts-root /abs/path/artifacts \
  --view trap
```

Run `summary` first to see current closure posture, `timeline` second to inspect guards and verifications, and `trap` last when the run appears to have halted unsafely.
