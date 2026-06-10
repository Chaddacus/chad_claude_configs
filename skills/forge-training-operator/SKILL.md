# Forge Training Operator

Use this skill when the task is to run, resume, inspect, or monitor Forge candidate/proof workflows, especially on `inference_box`.

## Goal

Operate Forge training at the highest useful layer:
- prefer the repo-native operator surface over raw API calls
- preserve exact run/proof lineage
- fail fast with explicit blockers
- stop safely at manual promotion points unless the user explicitly asks to publish

## Canonical Surface

Primary entrypoint:
- `/Users/chadsimon/code/zoom_slm_orchestration/scripts/forge_training_operator.py`

Underlying engine:
- `/Users/chadsimon/code/zoom_slm_orchestration/scripts/run_forge_candidate_cycle.py`

Live monitor:
- `/Users/chadsimon/code/forge/scripts/forge_live.py`

## Default Workflow

1. Inspect current state first.
   - Use `status` or `inspect-failure` before launching new work if there is an existing run or proof.
2. Prefer `proof-from-run` when there is already a completed seed run.
3. Prefer `candidate-run` only when a new training run is actually needed.
4. Keep auto-publish off unless the user explicitly asks to publish/promote.
5. For remote Forge on `inference_box`, pass:
   - `--forge-base-url http://127.0.0.1:28088` when using the local tunnel
   - `--forge-ssh-host inference_box`
   - `--forge-remote-host-root /root/web/forge-staging-gpu`
6. For live proof monitoring on `inference_box`, use:
   - `python3 scripts/forge_live.py --mode control-plane --control-plane-base-url http://127.0.0.1:8088 --proof-id <proof_id> <run_id>`

## Commands

Status:

```bash
uv run python /Users/chadsimon/code/zoom_slm_orchestration/scripts/forge_training_operator.py \
  --forge-base-url http://127.0.0.1:28088 \
  --control-plane-key "$FORGE_CONTROL_PLANE_KEY" \
  status \
  --run-id <run_id> \
  --proof-id <proof_id> \
  --out-dir <out_dir>
```

Inspect failure:

```bash
uv run python /Users/chadsimon/code/zoom_slm_orchestration/scripts/forge_training_operator.py \
  --forge-base-url http://127.0.0.1:28088 \
  --control-plane-key "$FORGE_CONTROL_PLANE_KEY" \
  --forge-ssh-host inference_box \
  --forge-remote-host-root /root/web/forge-staging-gpu \
  inspect-failure \
  --run-id <run_id> \
  --proof-id <proof_id> \
  --out-dir <out_dir>
```

Start proof from completed run:

```bash
uv run python /Users/chadsimon/code/zoom_slm_orchestration/scripts/forge_training_operator.py \
  --forge-base-url http://127.0.0.1:28088 \
  --control-plane-key "$FORGE_CONTROL_PLANE_KEY" \
  --forge-ssh-host inference_box \
  --forge-remote-host-root /root/web/forge-staging-gpu \
  proof-from-run \
  --run-id <run_id> \
  --out-dir <out_dir> \
  --skip-publish
```

Resume proof:

```bash
uv run python /Users/chadsimon/code/zoom_slm_orchestration/scripts/forge_training_operator.py \
  --forge-base-url http://127.0.0.1:28088 \
  --control-plane-key "$FORGE_CONTROL_PLANE_KEY" \
  resume-proof \
  --run-id <run_id> \
  --proof-id <proof_id> \
  --out-dir <out_dir>
```

## Required Behavior

- Always surface:
  - run id
  - proof id
  - current stage/status
  - latest blocker
  - latest eval run id
  - latest iteration id
  - latest child run id when available
- Treat `waiting/promote_candidate` with `auto_promote=false` as a successful manual-proof terminal state.
- Do not silently publish.
- If there is a concrete blocker like `worker_unavailable` or `invalid_holdout_eval_rows_path`, report that exact reason instead of a generic failure label.
