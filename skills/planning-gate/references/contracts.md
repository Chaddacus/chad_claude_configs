# Planning Gate Contracts

## Versioning
- Plan payloads must declare `schema_version: "plan.v1"`.
- Execution IR payloads must declare `schema_version: "execution-plan.v1"`.
- Implementation payloads must declare `schema_version: "implementation.v1"`.
- Unknown schema versions are fail-closed (`status=blocked`).

## Execution IR Boundary
- `plan.v1` is authoritative for outer intent and compatibility.
- `execution-plan.v1` is authoritative for execution semantics.
- `execution-plan.v1` is internal, compiled, and not user-authored.
- The kernel executes only `execution-plan.v1`, never `plan.v1` directly.
- The compiler is the only legal bridge from `plan.v1` to executable kernel input.
- If `plan.v1` cannot compile into valid `execution-plan.v1`, execution must not start.
- Any semantic disagreement between outer plan meaning and compiled IR meaning is a compile defect, not runtime discretion.
- The compiler must emit fully kernel-valid IR or fail; partial executable IR is not allowed.

## Runtime Transaction Boundary
- Planning-gate treats bootstrap and each kernel `step()` as a transaction boundary for authoritative runtime truth.
- The runtime stages authoritative post-state under `transactions/<txid>/staged/...` and commits that staged set in a fixed deterministic order.
- If a prepared or committing transaction has a complete valid staged payload, recovery must finish the commit idempotently.
- If staged payloads are missing, corrupted, or fail validation, recovery must:
  - write/update `objective.invalid-transition.json`
  - force kernel state to `unsafe`
  - mark the transaction `aborted`
  - halt further execution
- The runtime must never guess whether partially written live artifacts are authoritative.

Authoritative transactional artifact set:
- `objective.kernel-runtime-state.json`
- `objective.transition-history.jsonl`
- `objective.verification-results.jsonl`
- `objective.runtime-state.json`
- `objective.status.json`
- `objective.schedule.json`
- `objective.summary.json`
- `objective.support-confidence.json`
- `objective.execution-ledger.json`
- `objective.packet-results.jsonl`
- `objective.momentum.json`
- `objective.blockers.json`
- session `checkpoint`
- cycle `state` when a phase transition is part of the committed step result

Transaction-control artifacts:
- `objective.transaction-state.json`
- `objective.transaction-log.jsonl`
- `transactions/<txid>/staged/...`

Non-transactional-by-design artifacts:
- cycle request/result/review payloads
- capture manifests and stdout/stderr artifacts
- packet definition snapshots

Interpretation rules:
- non-transactional artifacts may exist without a committed authoritative step
- pre-transaction cycle request creation may leave request/state artifacts visible before authoritative step commit finishes
- replay/status must treat transaction artifacts as the source of truth for transaction state
- disagreement between transaction-control artifacts and authoritative runtime state is a runtime defect, not operator discretion

## Plan Contract
Required top-level fields:
- `schema_version`
- `objective`
- `intent_contract`
- `clarification_governor`
- `autonomous_session_readiness`
- `constraints`
- `scope_boundaries`
- `implementation_plan`
- `definition_of_done`
- `objective_requirements`
- `objective_coverage_map`
- `assumptions_ledger`
- `authority_map`
- `integration_map`
- `evidence_plan`
- `dependencies`
- `tests`
- `logging_plan`
- `rollback_plan`
- `risks`
- `approval_gate`
- `non_goals`
- `quality_bar`
- `decomposition_policy`
- `objective_closure_policy`
- `migration_fallback_policy`
- `scheduler_policy`
- `hardening_budget`
- `plan_status`
- `plan_gap_report`
- `pre_delivery_gap_review`
- `plan_sufficiency_report`
- `requirement_risk_rank`
- `momentum_map`
- `frontier_map`
- `autonomy_level`
- `packets`
- `session_harness`
- `required_packets`
- `execution_shape` for `R3`/`R4`

Conditional plan sections:
- `contract_closure` is required when the plan introduces persisted state, bootstrap/recovery behavior, new public API surface, duplicated materialized state, or new runtime/operator/control surfaces.
- `overengineering_guardrails` is required under the same trigger conditions.
- `contract_closure` must define:
  - `defined_terms`
  - `authority_boundary`
  - `repair_boundary`
  - `mutator_contracts`
  - `read_contracts`
  - `frozen_surfaces`
- `overengineering_guardrails` must define:
  - `minimum_value_loop`
  - `surface_budget`
  - `reuse_proof`
  - `deferred_surfaces`
  - `forbidden_growth`
  - `simplicity_tripwires`

Route-conditioned planning layer rule:
- `R3`/`R4` plans must include:
  - `execution_shape`
  - `solution_ladder`
  - `chosen_layer`
  - `layer_justification`
  - `why_not_lower`
  - `why_not_higher`
  - `future_reuse_gain`
  - `existing_primitives_considered`
  - `reuse_first_decision`
  - `estimated_files_touched`
  - `estimated_loc`
  - `budget_exception_justification`
  - `new_surface_proof`
- `solution_ladder` must compare:
  - `L1_patch`
  - `L2_abstraction`
  - `L3_operating_surface`
- `chosen_layer` must be the highest useful layer that is justified by recurrence/spread/operability and still bounded enough to implement now.
- `future_reuse_gain` must define:
  - `frequency` (`low`, `medium`, `high`)
  - `spread` (`single_flow`, `multi_flow`, `system_surface`)
  - `operability` (`local_only`, `reuse`, `operator_surface`)
  - `boundedness` (`bounded_now`, `bounded_follow_on`, `unbounded_now`)
- `R2` may stay lighter and does not fail solely for missing ladder fields, but should still consider abstraction or an operating surface when recurrence or multi-flow impact is obvious.
- `execution_shape` must be:
  - `single_lane`
  - `bounded_swarm`
- `R1`, `R2`, and `R5` use `single_lane`.
- `R3` defaults to `single_lane`.
- `R3`/`R4` may select `bounded_swarm` only when:
  - expected frontier width exceeds `1`
  - packet scopes are bounded enough for safe parallel progress
  - reviewer/verifier convergence points are explicit
  - expected throughput gain is real
  - `swarm_justification` explains why single-lane execution is insufficient
- Plans must remain within `3` files and `500 LOC` unless `budget_exception_justification` explicitly approves the larger scope.
- Plans must prefer existing primitives first; introducing a new service, persistence layer, schema family, or orchestration engine requires `new_surface_proof`.
- A plan is fail-closed when:
  - the ladder is missing for `R3`/`R4`
  - `chosen_layer` is lower than the highest useful layer implied by `future_reuse_gain`
  - `chosen_layer` is higher than justified by reuse/operability and boundedness
  - `bounded_swarm` is selected for an effectively serial or scope-entangled packet graph
  - a new runtime surface is proposed without proof an existing primitive is insufficient
  - the simplicity budget is exceeded without explicit exception approval
  - `contract_closure` is required but missing or incomplete
  - `overengineering_guardrails` is required but missing or incomplete
  - a mutator contract omits explicit write-set or reject behavior
  - a read contract omits explicit read-only semantics
  - frozen surfaces are not enumerated
  - over-engineering tripwires or deferred surfaces are missing for a thin-slice plan

Frontloaded planning alignment:
- Frontloaded Planning v2.5 is the pre-execution planning layer for Proxy-Managed Autonomous Completion v2.7.
- The plan compiler must emit `execution-plan.v1` as the canonical internal execution IR, plus any required review artifacts.
- Planning artifacts must preserve stable packet IDs, requirement IDs, and contract references so the compiler can normalize them without runtime remapping.
- A plan is not execution-ready unless it is both planning-sufficient and able to compile deterministically into `execution-plan.v1` without semantic drift.
- The first operator-facing `<proposed_plan>` must already be post-gap-review. “Ask the user to look for gaps” is not a valid fallback for a discoverable planning defect.

Execution-plan compiler invariants:
- Determinism:
  same normalized compile input must produce identical IR.
  The compile input boundary includes plan content, `track_id`, and any explicit allowed compiler options.
- Scope preservation:
  the IR may preserve or narrow authored execution scope, but must never add candidate files, packet authority, or execution targets unless explicit generated-validation rules allow it.
- Identity preservation:
  authored packet ids must remain stable through compile;
  generated validation packet ids may appear only through defined generated-validation rules;
  authored and generated identities must remain distinguishable deterministically.
- Compile failure:
  malformed outer plans, semantic drift, unauthorized scope widening, or invalid generated identity behavior must fail compilation rather than deferring ambiguity to runtime.

Canonical completion-governor rules:
- Safe momentum exists when at least one packet remains that is autonomy-ready, dependency-ready, policy-compliant, and expected to produce new evidence or advance the frontier without requiring unresolved authority decisions.
- New evidence means test results, verifier verdicts, changed packet state, frontier movement, artifact updates, dependency unlocks, or concrete blocker isolation. Restated reasoning or repeated commands alone do not count as evidence.
- An uncertainty-reducing packet is valid only if its expected evidence will either unlock additional runnable packets, resolve a dependency classification, or isolate a blocker that previously prevented closure.

Canonical packet validity rule:
- A packet is valid only if it has exactly one primary behavior, a bounded allowed scope, explicit dependency mode, objective acceptance checks, diagnosable failure signals, and a maintainable completion path.

Canonical autonomy-readiness rule:
- A packet is autonomy-ready only if product meaning is already resolved, all dependencies satisfy the declared dependency mode, acceptance checks are automatable, no prohibited action is required, and the packet can be completed within allowed scope.

Canonical maintainability rule:
- A maintainable completion path means the packet can be completed without unauthorized debt, hidden product decisions, unjustified duplication, or out-of-policy architectural drift.

Required `definition_of_done[]` contract:
- At least 5 items.
- Required fields per item:
  - `id`
  - `category`
  - `criterion`
  - `verification`
- Required categories (all must be present):
  - `correctness`
  - `tests`
  - `security`
  - `observability`
  - `rollback`
- `criterion` and `verification` must be concrete and non-trivial (`minLength` enforced).
- `id`, `category`, `criterion`, and `verification` must be strings.
- `verification` must be executable or artifact-based (command/script/file evidence), not generic prose.
- Missing/invalid categories or weak/blank entries produce `status=revise`.

Required governed-control-plane additions:
- `non_goals` must be a non-empty list of explicit scope boundaries.
- `quality_bar` must define `maintainability`, `evidence`, and `policy_compliance`.
- `objective_closure_policy` must define `allowed_states` and `boundary_shrink_allowed`.
- `migration_fallback_policy` must define `compat_fallback_allowed`, `max_fallback_invocations`, and `manifest_rollback_path`.
- `hardening_budget` must define:
  - `max_hardening_passes`
  - `max_repacketization_passes`
  - `max_unresolved_gaps_before_revise`
  - `max_authority_blockers_before_blocked`
- `plan_status` must be one of:
  - `draft`
  - `hardening`
  - `execution_ready_candidate`
  - `execution_ready`
  - `revise`
  - `blocked`
- `plan_gap_report` must define:
  - `gaps_detected`
  - `gaps_auto_fixed`
  - `gaps_escalated`
  - `gaps_unresolved`
- `pre_delivery_gap_review` must define:
  - `performed`
  - `issues_found`
  - `issues_fixed`
  - `issues_remaining`
  - `ready_to_present`
  - `review_summary`
- `pre_delivery_gap_review` is fail-closed when:
  - `performed` is not `true`
  - `ready_to_present` is not `true`
  - `issues_remaining` is non-empty
  - the review summary is missing or trivial
- `plan_sufficiency_report` must define:
  - `status`
  - `coverage_complete`
  - `integration_realism`
  - `runtime_compatible`
  - `unresolved_gap_count`
  - `verifier_notes`
- `requirement_risk_rank` must define, per requirement:
  - `requirement_id`
  - `priority` (`core`, `critical`, `optional`)
  - `associated_packet_ids`
  - `evidence_type`
  - `failure_impact`
- `intent_contract` must define:
  - `objective`
  - `success_criteria`
  - `non_goals`
  - `authority_sensitive_decisions`
  - `ambiguity_classification`
  - `objective_shape_status`
- `clarification_governor` must define:
  - `default_batch_limit`
  - `allowed_topics`
  - `repo_discoverable_questions_forbidden`
  - `new_authority_boundary_required_for_mid_execution_clarification`
- `autonomous_session_readiness` must define:
  - `status`
  - `readiness_gaps`
  - `safe_momentum_ready`
  - `next_executable_frontier`
- `momentum_map` must define:
  - `unlocking_packets`
  - `uncertainty_reducing_packets`
  - `blocker_isolating_packets`
  - `escalation_candidate_packets`
- `frontier_map` must define:
  - `current_frontier`
  - `parallel_safe_packets`
  - `sequence_required_packets`
  - `blocked_packets`
  - `deferred_packets`
- `autonomy_level` must be one of:
  - `L1_guided`
  - `L2_supervised_autonomous`
  - `L3_supervised_throughput`
  - `L4_release_adjacent`
- `scheduler_policy` must define:
  - `max_parallel_packets`
  - `parallelism_policy`
  - `admission_rule`
  - `recompute_triggers`
  - `terminal_stop_conditions`
  - optional `lane_caps`
  - optional `route_swarm_cap`
  - optional `frontier_dispatch_order`
  - optional `reviewer_barrier_points`
  - optional `convergence_required_for_closure`
- `session_harness` must define:
  - `required`
  - `route_hint`
  - `estimated_packet_count`
  - `expected_duration_minutes`
  - `checkpoint_interval_minutes`
  - `checkpoint_required`
  - `context_index_required`
  - `bootstrap_commands`
  - `validation_commands`
  - `clean_state_assertions`
  - `ui_evidence_required`
- `packets` must be a valid packet DAG.
- `objective_requirements` must be a stable-ID list of requirement objects.
- `objective_coverage_map` must map every requirement to packet(s), verification, and evidence.
- `assumptions_ledger` must classify each assumption as `resolved_from_context`, `reversible_technical`, `authority_required`, or `blocked`.
- `assumptions_ledger[]` must define:
  - `assumption_id`
  - `statement`
  - `classification`
  - `disposition`
- `integration_map` must map every integration touchpoint to packets and verification/evidence.
- `failure_mode_matrix` and `edge_case_matrix` must be non-empty for implementation planning.
- `objective_requirements[]` must define:
  - `requirement_id`
  - `description`
  - `priority` (`core`, `critical`, `optional`)
  - `definition_of_done`
- Every objective requirement `definition_of_done` must define:
  - `behavior_outcome`
  - `acceptance_checks`
  - `evidence_requirements`
  - `allowed_scope`
  - `rollback_or_fallback`
  - `verifier_acceptance_condition`
  - `objective_linkage`
- `authority_map[]` must define:
  - `authority_id`
  - `type`
  - `scope`
  - `resolution`
- `integration_map[]` must define:
  - `integration_id`
  - `touchpoint`
  - `packet_ids`
  - `verification`
  - `evidence`
- `failure_mode_matrix[]` and `edge_case_matrix[]` must define:
  - `item_id`
  - `scenario`
  - `handling`
  - `verification`

Packet contract requirements:
- Required fields:
  - `packet_id`
  - `primary_behavior`
  - `execution_mode`
  - `allowed_scope`
  - `dependencies`
  - `dependency_mode`
  - `acceptance_checks`
  - `failure_signals`
  - `constraints`
  - `fallback_or_rollback`
  - `classification`
  - `definition_of_done`
  - optional `packet_lane`
  - optional `swarm_eligible`
  - optional `parallelism_class`
  - optional `lane_affinity`
  - optional `preferred_agent_type`
- `packet_lane` values:
  - `explorer`
  - `worker`
  - `validator`
  - `reviewer`
- `parallelism_class` values:
  - `isolated`
  - `bounded`
  - `serial`
- `reviewer` packets must never be `isolated`.
- `swarm_eligible=false` packets must stay outside concurrent swarm dispatch.
- `serial` packets create a conflict barrier in their scope domain.
- `definition_of_done` must define:
  - `behavior_outcome`
  - `acceptance_checks`
  - `evidence_requirements`
  - `allowed_scope`
  - `rollback_or_fallback`
  - `verifier_acceptance_condition`
  - `objective_linkage`
- Dependency rules:
  - `dependency_mode=accepted_upstream`: every declared dependency must resolve to another packet id and every declared upstream dependency must be verifier-accepted before the dependent packet becomes runnable.
  - `dependency_mode=explicit_stub`: packet must declare explicit stub dependencies instead of implicit runtime discovery.
- DAG rules:
  - cycles are blocked
  - duplicate packet ids are blocked
  - missing dependency packet ids are blocked
- Packets failing current runtime validity, autonomy-readiness, or maintainability rules must be rewritten or split before approval.
- A plan is not execution-ready unless every autonomy-ready packet is expected to satisfy the v2.7 scheduler admission rule:
  - packet validity
  - autonomy readiness
  - dependency readiness
  - conflict check
  - retry-budget feasibility

Required smoke gates:
- Source of truth: `tests.smoke_gates` (top-level `smoke_gates` can be parsed for compatibility but is flagged and returns `status=revise`).
- Required stages: `25%`, `50%`, `75%`, `100%`.
- Required fields per stage:
  - `stage`
  - `status`
  - `criteria`
  - `commands`
  - `expected_output`
  - `failure_interpretation`
  - `proceed_decision`
  - `rollback_decision`
- `100%` stage must be `pass` (or `passed|ok|done`) to approve.

Required plan artifacts:
- `planning_artifacts/<track-id>/plan.intent.json`
- `planning_artifacts/<track-id>/plan.compiler.json`
- `planning_artifacts/<track-id>/plan.gaps.json`
- `planning_artifacts/<track-id>/plan.coverage.json`
- `planning_artifacts/<track-id>/plan.sufficiency.json`
- `planning_artifacts/<track-id>/plan.readiness.json`
- `planning_artifacts/<track-id>/objective.session.json`
- `planning_artifacts/<track-id>/objective.feature-list.json`
- `planning_artifacts/<track-id>/objective.progress.jsonl`
- `planning_artifacts/<track-id>/objective.checkpoint.json`
- `planning_artifacts/<track-id>/objective.context-index.json`
- `planning_artifacts/<track-id>/objective.momentum.json`
- `planning_artifacts/<track-id>/objective.blockers.json`
- These artifacts must be structurally consistent with runtime artifacts so planning and execution refer to the same objective and packet graph.
- `validate_plan.py` only approves plans already marked `execution_ready` and backed by a plan sufficiency artifact with verifier approval and runtime compatibility checks passing.

## Implementation Contract
Required top-level fields:
- `schema_version`
- `summary`
- `changed_files`
- `tests_run`
- `smoke_results`
- `logging_evidence`
- `rollback_validation`
- `memory_retrieval_evidence`
- `preferences_applied`
- `skill_trigger_eval_results`
- `prompt_contract_used`
- `frontend_roundtrip_evidence`
- `objective_status`
- `schedule_artifact`
- `packet_verdicts`
- `checkpoint_commit`
- `checkpoint_blocked`
- `checkpoint_block_reason`
- `checkpoint_block_evidence`
- `bootstrap_commands`
- `validation_commands`
- `clean_state_assertions`
- `migration_fallback`
- `budget_outcome`

`tests_run[]` required fields:
- `name`, `command`, `status`, `result`, `proof_artifact`, `proof_hash`

`smoke_results[]` required fields:
- `stage`, `status`, `command`, `observed_output`, `decision`, `proof_artifact`, `proof_hash`

`logging_evidence[]` required fields:
- `event`, `proof_artifact`, `proof_hash`

`rollback_validation` required fields:
- `executed`, `result`, `evidence`, `proof_artifact`, `proof_hash`

`memory_retrieval_evidence[]` required fields:
- `tool`, `query`, `result_count`
- Must be non-empty for non-trivial tasks.

`preferences_applied[]` required fields:
- `key`, `decision`, `rationale`
- Must be non-empty for non-trivial tasks.

`skill_trigger_eval_results[]` required fields:
- `skill`, `false_positive_rate`, `false_negative_rate`, `threshold_passed`
- Thresholds are fail-closed:
  - `false_positive_rate <= 0.10`
  - `false_negative_rate <= 0.10`
  - `threshold_passed = true`

`prompt_contract_used[]` required fields:
- `name`, `required_context`, `required_constraints`, `verification_section`, `done_when`
- Missing constraints or verification text is `status=revise`.

`frontend_roundtrip_evidence[]` required fields:
- `step`, `evidence`
- Required (non-empty) when frontend scope is detected from changed files, prompt contracts, or explicit frontend evidence.

`budget_outcome` required fields:
- `planned_files_touched`
- `planned_loc`
- `actual_files_touched`
- `actual_loc`
- `exception_used`
- `exception_justification`
- `proof_artifact`
- `proof_hash`

`objective_status` required fields:
- `objective_id`
- `closure_state`
- `completed_packets`
- `pending_packets`
- `blocked_packets`
- `deferred_packets`
- `boundary_shrunk_remainder`
- `artifact_path`

`schedule_artifact` required fields:
- machine-readable JSON artifact under `planning_artifacts/<track-id>/...` containing:
  - `objective_id`
  - `max_parallel_packets`
  - `parallelism_policy`
  - `dispatch_history`
  - `runnable_set`
  - `blocked_set`
  - `retry_counters`
  - `strategy_switches`
  - `repacketization_events`
  - `current_frontier`
  - `closure_readiness`
  - `total_packet_count`
  - `accepted_packet_count`
  - `rejected_packet_count`
  - `blocked_packet_count`
  - `repacketization_count`
  - `escalation_count`
  - `migration_fallback_used`
  - `total_runtime_attempts`

`packet_verdicts[]` required fields:
- `packet_id`
- `runtime_state`
- `verifier_output`
- `allowed_scope_status`
- `artifact_path`

`migration_fallback` required fields:
- `used`
- `reason`
- `artifact_path`

Harness continuity requirements:
- Harnessed objectives must keep `objective.feature-list.json`, `objective.progress.jsonl`, `objective.checkpoint.json`, and `objective.context-index.json` consistent with the active packet graph.
- Accepted packets require either:
  - a checkpoint commit recorded in both implementation metadata and `objective.checkpoint.json`, or
  - `checkpoint_blocked=true` plus explicit reason and evidence.
- `objective.checkpoint.json` required fields:
  - `objective_id`
  - `track_id`
  - `checkpoint_id`
  - `last_verified_packet_ids`
  - `current_frontier`
  - `bootstrap_commands`
  - `validation_commands`
  - `repo_state_summary`
  - `clean_state_assertions`
  - `next_recommended_packet`
  - `open_risks`
  - `handoff_notes`
- `objective.progress.jsonl` must be machine-readable JSONL and end with a checkpoint event consistent with the current checkpoint artifact.

Objective-closure rules:
- Valid closure states:
  - `OBJECTIVE_COMPLETE`
  - `OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK`
  - `OBJECTIVE_BLOCKED_ESCALATION_REQUIRED`
  - `OBJECTIVE_BLOCKED_MIGRATION_DEFECT`
  - `OBJECTIVE_REJECTED_FALSE_COMPLETION`
- Accepted completion mappings:
  - `OBJECTIVE_COMPLETE` -> `ACCEPTED_SUCCESS`
  - `OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK` -> `ACCEPTED_BLOCKED`
  - `OBJECTIVE_BLOCKED_ESCALATION_REQUIRED` -> `ACCEPTED_BLOCKED`
  - `OBJECTIVE_BLOCKED_MIGRATION_DEFECT` -> `ACCEPTED_BLOCKED`
  - `OBJECTIVE_REJECTED_FALSE_COMPLETION` -> blocked at finalize time

## Proof and Provenance
`proof_artifact` must:
- exist under `planning_artifacts/<track-id>/...`
- be a JSON manifest produced by `run_cmd_capture.py`
- declare:
  - `schema_version: "run-cmd-capture.v1"`
  - `producer: "run_cmd_capture.v1"`

`proof_hash` must be SHA256 of the manifest file bytes.
Proof manifests are bound to a single `track_id`; cross-track reuse is blocked.
Track IDs are normalized to a safe token form (spaces/special chars become `-`) before binding checks.
If a payload marks test/smoke/rollback result as pass, the proof manifest `exit_code` must be `0`.
Adaptive-memory and prompt evidence arrays are deterministic gate inputs and cannot be omitted.

## Command Safety Policy
Blocked by default:
- `rm -rf /`
- destructive git rollback commands without explicit approval
- `git checkout --`
- `curl ... | sh` / `wget ... | sh`
- recursive root-permission escalation patterns

Override requires both:
- `--allow-dangerous`
- `--reason "<why this is necessary>"`

## Review Statuses
- `approve`: all deterministic gates passed
- `revise`: contract/evidence gaps remain
- `blocked`: schema/proof/policy violation (fail-closed)

## Finalization
`finalize_gate.py` recomputes deterministic checks from `plan_json + impl_json + review_json`.
It never trusts prior `review.status` by itself.
Provide `--track-id` (or ensure it is present in `review.meta.track_id`) so proof binding is enforced at finalize time.

## Enterprise Quality Baseline
- Plans must encode enterprise-level Definition of Done criteria before coding begins.
- Implementation must execute autonomously through all gate commands and provide artifact-backed proof for final approval.
