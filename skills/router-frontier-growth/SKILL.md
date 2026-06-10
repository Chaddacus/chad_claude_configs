# Router Frontier Growth

Use this skill when the task is to operate the knowledge-expert router frontier growth loop: add the next 3 eligible articles, validate full article expansion packets, run the bounded tuning schedule, and report whether the frontier promoted or blocked.

## Goal

Operate at the repo-native surface:
- use the frontier state and growth CLI instead of ad hoc training commands
- fail closed on incomplete article packets
- preserve exact batch, run, eval, and fixture lineage
- summarize blocking buckets and knob packs tried

## Canonical Surface

Primary entrypoint:
- `/Users/chadsimon/code2/knowledge-expert/scripts/router-grow-frontier.ts`

Managed pilot engine:
- `/Users/chadsimon/code2/knowledge-expert/scripts/router-manage-pilot.ts`

## Default Workflow

1. Inspect current frontier state first.
   - Read `/Users/chadsimon/code2/knowledge-expert/benchmarks/research/router/frontier/latest.json` if it exists.
2. Run the next frontier batch through the repo CLI.
3. Report:
   - candidate article ids
   - packet validation status
   - knob packs tried
   - best attempt
   - pass/fail decision
   - exact failing buckets if blocked
4. Do not present an incomplete packet batch as a model regression.
5. Do not mutate live router/provider behavior.

## Commands

Default MLX run:

```bash
cd /Users/chadsimon/code2/knowledge-expert
npm run router:grow-frontier -- --backend=mlx --python-bin=/Users/chadsimon/code/forge/.venv/bin/python3 --base-model-ref=/Users/chadsimon/code2/knowledge-expert/models/base/qwen2.5-3b-instruct-4bit
```

Fast local linear run:

```bash
cd /Users/chadsimon/code2/knowledge-expert
npm run router:grow-frontier -- --backend=linear --base-model-ref=qwen-test --python-bin=python3
```

Explicit article batch:

```bash
cd /Users/chadsimon/code2/knowledge-expert
npm run router:grow-frontier -- --backend=linear --article-ids=alpha|beta|gamma --base-model-ref=qwen-test --python-bin=python3
```

## Required Behavior

- Always surface:
  - current frontier size
  - attempted article ids
  - packet counts per article
  - packet validation result
  - attempted knob packs
  - promoted run/eval ids or blocking buckets
- Treat `incomplete_packet` as a preflight/content failure, not a training failure.
- Keep repo logic in the CLI and frontier state artifacts, not in the skill text.
