---
name: planning-gate
description: Use when work is non-trivial and requires deterministic, fail-closed quality gates for planning and implementation evidence before final approval.
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names,destructive_rollback,branch_policy_live
---

# Planning Gate

This skill is the canonical operator workflow for planning-gate. Global and workspace `CLAUDE.md` files require its use; they do not restate the full procedure.

## Use When

- the task touches more than 2 files
- the task affects auth, security, migrations, or production behavior
- you need auditable proof for tests, smoke checks, rollback, or governed closeout

## Canonical Inputs

- runtime contract source:
  - `/Users/chadsimon/.claude/state/route_manifest.json`

## Required Workflow

1. **Write the plan.** Before presenting, run a silent gap review and fix discoverable incompleteness. If real ambiguity remains, ask only the minimal blocking question. The plan must include: objective, acceptance criteria per packet (each must be falsifiable — "X works" is not a criterion; "running `npm test` returns exit 0" is), solution ladder (L1_patch / L2_abstraction / L3_operating_surface) with chosen layer and justification (why_not_lower, why_not_higher), existing_primitives_considered, reuse_first_decision, estimated_files_touched, estimated_loc. If the plan introduces persisted state, new public API surfaces, or new runtime/operator surfaces, include overengineering_guardrails with explicit mutator/read contracts, frozen surfaces, and simplicity tripwires. Choose `single_lane` by default; `bounded_swarm` only when a real decomposition frontier exists and can be justified in one sentence.

2. **Sprint contract.** From the plan, emit a concise list (≤8 bullets) of testable acceptance criteria. Send to reviewer for explicit ack before execution begins. No execution starts without reviewer ack. An ack is a binding commitment to evaluate against those criteria at closure. Criteria cannot change after ack without a new ack cycle.

3. **Execute** packets in dependency order per the plan. Implement → test → fix per slice.

4. **Produce implementation evidence**: diff, test output (pass/fail with file:line references for failures), and a criterion-by-criterion mapping showing how each acceptance criterion is satisfied with evidence.

5. **Run `validate_impl.py`** against plan + implementation evidence. Address all missing or blocked fields before proceeding.

6. **Run `finalize_gate.py`.** Must return `ok=true`. Do not treat work as approved without it. If blocked, fix identified fields and re-run.

## Commands

```bash
python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/validate_impl.py" \
  --plan-json /abs/path/plan.json \
  --impl-json /abs/path/implementation.json \
  --review-json-out /abs/path/review.impl.json \
  --track-id task-123

python3.11 "${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/planning-gate/scripts/finalize_gate.py" \
  --plan-json /abs/path/plan.json \
  --impl-json /abs/path/implementation.json \
  --review-json /abs/path/review.impl.json \
  --track-id task-123 \
  --out /abs/path/finalize.json
```

## Runtime Diagnosis

If `finalize_gate.py` returns `ok=false`, read its `blocked_fields` and `missing_fields` output. Address each item, regenerate implementation evidence, re-run `validate_impl.py`, then re-run `finalize_gate.py`. Repeat until `ok=true`.

## Safety Rules

- Python runtime must be `>=3.11`.
- Proof artifacts must live under `planning_artifacts/<track-id>/`.

## Ownership Boundary

This skill owns the procedural planning-gate workflow. It does not own:
- global git policy
- route selection policy
- branch naming policy
- general review formatting rules

Those live in:
- `/Users/chadsimon/.claude/CLAUDE.md`
- project/workspace `CLAUDE.md`
- `/Users/chadsimon/.claude/state/route_manifest.json`
