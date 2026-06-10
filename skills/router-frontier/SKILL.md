---
name: router-frontier
description: Drive the router frontier growth state engine through select → generate → train → evaluate → diagnose → tune → promote cycles
user-invocable: true
---

# Router Frontier State Engine Skill

## Overview

This skill drives the router frontier growth state engine — a finite state machine that progressively expands the router training dataset by selecting new articles, training models, evaluating against quality gates, diagnosing failures, and promoting successes.

The TypeScript state engine (`src/router-training/frontier-state-engine.ts`) owns transitions, guards, and persistence. The step CLI (`scripts/router-frontier-step.ts`) executes one phase per invocation. This skill instructs you how to drive them.

## Usage Modes

### `/router-frontier status`
Report current frontier state: accepted articles, blocked batches, active phase.

```bash
npm run router:frontier-step -- --phase status
```

### `/router-frontier grow`
Run a complete growth cycle from the current state through to promotion or hard block.

### `/router-frontier resume`
Pick up from whatever phase was interrupted. Read the status output and continue from the reported `currentPhase`.

### `/router-frontier diagnose`
Analyze the latest blocked batch — review diagnostic context and recommend fixes.

## State Machine

```
[idle] → [select] → [generate] → [train] → [evaluate]
                        ^            ^          │
                        │            │     pass │ fail
                        │       [tune] ←──[diagnose]
                        │                    │  │
                        └── (data fix) ──────┘  │
                                                │
                     (retries exhausted) ───────►│
                                                │
                  [hard_blocked] ←──────────────┘
                        │
                  [advance] → [idle]
                        │
                  [promote] → [advance] → [idle]
```

## Phase Procedures

### STATUS
```bash
npm run router:frontier-step -- --phase status
```
Read the output JSON. Key fields:
- `currentPhase`: where we are
- `acceptedFrontierSize`: how many articles in the frontier
- `activeBatchContext`: details of in-progress batch (null if idle)
- `nextPhases`: what phases are valid next

### SELECT
```bash
npm run router:frontier-step -- --phase select
```
Picks the next 3 eligible articles from inventory. Transitions to `generating` automatically.
If fewer than 3 articles are eligible, returns `blocked`.

### GENERATE
```bash
npm run router:frontier-step -- --phase generate
# With supplements (after LLM review):
npm run router:frontier-step -- --phase generate --supplements-file /path/to/supplements.json
```
Builds expansion packets from article bundles. Validates query coverage.
Returns `packetReviewContext` with per-query relevance signals.

**LLM Review Protocol (always perform):**
1. Read the `packetReviewContext` from generate output
2. Check for `weak: true` queries — these have poor relevance signals
3. Replace weak queries with better alternatives that reference article-specific terms
4. Fill any gaps in coverage (seen, heldout, confuser, abstain)
5. Ensure confuser queries genuinely discriminate between frontier articles
6. Write supplements JSON and re-run generate with `--supplements-file`

Supplements JSON format:
```json
[{
  "articleId": "article_123",
  "replaceQueries": {
    "seenQueries": ["Better query 1", "Better query 2"],
    "abstainBroadNegativeQuery": ["How do I share screen?"]
  },
  "addQueries": {
    "heldoutQueries": ["Additional heldout query"]
  }
}]
```

### TRAIN
```bash
# From scratch (default)
npm run router:frontier-step -- --phase train --backend linear --python-bin python3 --base-model-ref qwen-test
# Resume from checkpoint (near miss)
npm run router:frontier-step -- --phase train --backend mlx --resume-from /path/to/checkpoint --python-bin ... --base-model-ref ...
```

**Training Strategy Decision:**
- **Default (scratch)**: Use when the prior run showed fundamental issues or this is the first attempt
- **Checkpoint resume**: Use when DIAGNOSE reported `nearMiss: true` — the model nearly passed and needs refinement, not a fresh start. The `bestCheckpointPath` from diagnostic context points to the adapter to resume from.

### EVALUATE
```bash
npm run router:frontier-step -- --phase evaluate
```
Evaluates the latest training run against frontier gates. Returns:
- `passed: true` → proceed to PROMOTE
- `passed: false` → automatically transitions to DIAGNOSE; check `retriesRemaining`

### DIAGNOSE
```bash
npm run router:frontier-step -- --phase diagnose
```
Builds diagnostic context from the failed evaluation. Returns:
- `failureCategories`: what went wrong (abstain_weakness, selection_confusion, incumbent_regression, new_article_weak)
- `suggestedKnobAdjustments`: deterministic knob fixes based on failure category
- `nearMiss`: true if only 1-2 buckets failed by <5%
- `retriesRemaining`: budget left

**Diagnostic Protocol:**
1. Read `failureCategories` to understand the dominant problem
2. Check `priorAttempts` to see what's already been tried
3. Decide fix type:
   - **Knob fix** → proceed to TUNE (training config changes)
   - **Data fix** → proceed to GENERATE with supplements (query quality changes)
   - **Exhausted** → proceed to advance (hard block)
4. If `abstain_weakness` dominates: increase distillation weights, review abstain query quality
5. If `selection_confusion` dominates: check confuser overlap, improve article profile separation
6. If `incumbent_regression`: preserve incumbent signal, reduce new article weight
7. If `new_article_weak`: increase capacity (rank, epochs), improve query coverage

### TUNE
```bash
# With explicit knob overrides
npm run router:frontier-step -- --phase tune --knob-env '{"ROUTER_MLX_ANSWER_AUX_LOSS_WEIGHT":"1.50"}'
# Auto-synthesize from diagnostic context
npm run router:frontier-step -- --phase tune
```
Applies knob adjustments. If no `--knob-env` provided, auto-synthesizes from the diagnostic context's suggested adjustments.

### PROMOTE
```bash
npm run router:frontier-step -- --phase promote
```
Merges candidates into the accepted frontier. Only run after EVALUATE passes.

### ADVANCE
```bash
npm run router:frontier-step -- --phase advance
```
Resets active batch and returns to idle. Used after PROMOTE or HARD_BLOCK.

## Retry Budget

- **Attempts 1-3**: Static knob packs (baseline → answer_heavier → abstain_lighter)
- **Attempts 4-5**: LLM-synthesized knob configurations from diagnostic analysis
- After 5 failures: HARD_BLOCK

## Backend Strategy

1. Use `--backend linear` for fast iteration during DIAGNOSE/TUNE loops (~400ms per cycle)
2. Switch to `--backend mlx` only when linear passes gates (real promotion requires MLX)
3. If MLX fails where linear passed, allow 2 additional MLX-specific diagnostic attempts

## Safety Rails

- Never weaken gate thresholds to force promotion
- Never promote on linear backend alone — MLX confirmation required for production
- Never skip DIAGNOSE after a failed EVALUATE
- Stop and report if 2 consecutive batches HARD_BLOCK on the same failure category

## Evidence Requirements

- Every phase must produce a runnable CLI command and its JSON output
- PROMOTE requires: passing metrics snapshot, runId, evalId, all bucket pass rates
- HARD_BLOCK requires: all attempted knob packs, best metrics achieved, dominant failure category

## Knob Surface Reference

| Failure Category | Primary Knobs | Direction |
|---|---|---|
| `abstain_weakness` | `ABSTAIN_AUX_LOSS_WEIGHT` ↑, `BROAD_NEGATIVE_SIGMOID_DENOM` ↓, `NONE_OF_THESE_WEIGHT` ↑ | Strengthen abstain signal |
| `selection_confusion` | `ANSWER_AUX_LOSS_WEIGHT` ↑, `MAX_LEARNING_RATE` ↓ | Sharpen selection boundaries |
| `incumbent_regression` | `ANSWER_AUX_LOSS_WEIGHT` ↑, `ABSTAIN_AUX_LOSS_WEIGHT` ↓ | Preserve incumbent routing |
| `new_article_weak` | `ANSWER_AUX_LOSS_WEIGHT` ↑ | More capacity for new patterns |
