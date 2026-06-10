# Planning Gate Commands

## Commands

```bash
python3 "$CODEX_HOME/skills/planning-gate/scripts/compile_intent.py" \
  --plan-json /abs/path/plan.json \
  --review-json-out /abs/path/review.plan.intent.json \
  --track-id task-123

python3 "$CODEX_HOME/skills/planning-gate/scripts/initialize_session.py" \
  --plan-json /abs/path/plan.json \
  --review-json-out /abs/path/review.plan.init.json \
  --track-id task-123

python3 "$CODEX_HOME/skills/planning-gate/scripts/compile_plan.py" \
  --plan-json /abs/path/plan.json \
  --review-json-out /abs/path/review.plan.compile.json \
  --track-id task-123

python3 "$CODEX_HOME/skills/planning-gate/scripts/verify_plan.py" \
  --plan-json /abs/path/plan.json \
  --review-json-out /abs/path/review.plan.verify.json \
  --track-id task-123

python3 "$CODEX_HOME/skills/planning-gate/scripts/validate_plan.py" \
  --plan-json /abs/path/plan.json \
  --review-json-out /abs/path/review.plan.json \
  --track-id task-123

python3 "$CODEX_HOME/skills/planning-gate/scripts/run_cmd_capture.py" \
  --track-id task-123 \
  --stage 50% \
  --name unit-tests \
  --cwd /abs/path/repo \
  -- pytest -q

python3 "$CODEX_HOME/skills/planning-gate/scripts/validate_impl.py" \
  --plan-json /abs/path/plan.json \
  --impl-json /abs/path/implementation.json \
  --review-json-out /abs/path/review.impl.json \
  --track-id task-123

python3 "$CODEX_HOME/skills/planning-gate/scripts/finalize_gate.py" \
  --plan-json /abs/path/plan.json \
  --impl-json /abs/path/implementation.json \
  --review-json /abs/path/review.impl.json \
  --track-id task-123 \
  --out /abs/path/finalize.json
```
