---
policy_doc_kind: workflow
classification: canonical
canonical_owner: self
authority_level: procedural
---

# Obsessive Loop — Orchestrator/Worker Workflow v1.0

The high-leverage operating model for autonomous improvement of a target repo.
The **brain** (Opus high-effort, in the chad-twin / claude session) drives;
goose (GPT-5.5 via ACP) is the workhorse.

This is **not** the legacy `obsessive_loop.py` autonomous state machine. That
script is retained for unattended overnight runs but is not the canonical flow.

## Roles

- **Orchestrator** = the chad-twin LLM session. Plans, dispatches, reviews
  diffs and reports, decides accept/reject/replan, commits, iterates.
  Reasoning happens in the LLM's context, not in Python.
- **Worker** = goose, dispatched fresh per slice via `goose_dispatch.py`.
  Implements one slice, runs tests, exits. Cheap (Pro/Max subscription path).
- **Coordination layer** = three primitives:
  - `~/.claude/bin/goose_dispatch.py` — already exists; orchestrator → goose.
  - `~/.claude/bin/presets/obsessive_slice_verify.sh` — acceptance preset
    that goose_dispatch runs at end-of-slice. Classifies breadth, runs tests
    at required breadths, re-runs rubric, computes delta vs baseline, emits
    `SliceReport.json`. Exit 0 only if breadths run clean and no rubric
    regression.
  - `~/.claude/bin/obsessive_slice_state.py` — JSONL-backed slice lifecycle
    state. `init`, `register`, `next`, `mark`, `summary`.

## Loop

```
1. Onboard target (one-time): cw-ai-kickstarter/recipes/dogfood-harness.sh <repo>
2. Initialize run:
     ./obsessive_slice_state.py init --repo <repo> --branch codex/obsessive-<repo>-<ts>
   Captures baseline scorecard from .artifacts/rubric-suite/scorecard.json.
   Returns run_id.
3. Create worktree:
     git -C <repo> worktree add -b codex/obsessive-<repo>-<ts> <worktree-path>
4. PLAN (orchestrator): read scorecard + targeted code, decompose into 3-7
   slices with dependencies. Each slice = single objective, target category,
   files of interest, success criteria, do-not-touch list.
5. REGISTER each planned slice:
     ./obsessive_slice_state.py register --run <run> --slice <id> --spec <json>
6. DISPATCH LOOP:
     a. ./obsessive_slice_state.py next --run <run>   → next runnable slice
     b. record base_sha to <run-dir>/reports/<slice>/.base_sha
     c. goose_dispatch.py \
          --slice-id <id> \
          --workspace <worktree> \
          --spec "<imperative goose prompt>" \
          --brief "<scoped brief>" \
          --files <hint1>,<hint2> \
          --acceptance-script ~/.claude/bin/presets/obsessive_slice_verify.sh \
          --preset-args "<id> <baseline-scorecard> <run-dir>/reports/<slice>"
     d. Read goose_dispatch's stdout JSON envelope.
     e. Read SliceReport.json from <run-dir>/reports/<slice>/report.json.
     f. Read the diff at .../diff.patch — orchestrator's own review.
     g. DECIDE:
        - Accept: git -C <worktree> commit -am "<id>: <objective>"
                  ./obsessive_slice_state.py mark --status accepted \
                      --commit-sha <sha> --rubric-delta <pp>
        - Reject (recoverable): re-dispatch with retry feedback baked in
                                  (goose_dispatch already supports this via
                                  attempt N+1 with verify-tail in user prompt)
        - Block: ./obsessive_slice_state.py mark --status blocked --reason <why>
        - Replan: revise upcoming slices (re-register or skip) before continuing
     h. Repeat until all accepted/blocked or wallclock cap.
7. SUMMARIZE: ./obsessive_slice_state.py summary --run <run>
8. (optional) Merge codex/obsessive-<repo>-<ts> into the target's main branch.
```

## SliceSpec

Free-form fields driven by orchestrator judgement, plus a few structured keys:

```json
{
  "slice_id": "obs-omni-001",
  "objective": "<imperative goose prompt — first-person 'do X'>",
  "rationale": "<orchestrator's reasoning, multi-paragraph, not seen by goose>",
  "target_category": "observability",
  "target_files_hint": ["packages/cli/src/**/*.ts"],
  "do_not_touch": ["packages/dashboard/**"],
  "success_criteria": [
    "design/observability category score >= 4",
    "all existing tests pass",
    "no rubric regression elsewhere (delta_pp >= -0.5)"
  ],
  "depends_on": ["obs-omni-000"],
  "max_attempts": 2
}
```

## SliceReport

Worker-produced (by `obsessive_slice_verify.sh`). Deterministic. The
orchestrator validates BOTH this AND the diff itself.

```json
{
  "slice_id": "obs-omni-001",
  "status": "completed" | "failed",
  "base_sha": "...",
  "head_sha": "...",
  "breadth_required": ["full"],
  "tests": {"full": {"command": "...", "exit": 0, "passed": 142, "failed": 0}},
  "rubric_delta": {
    "weighted_avg_before": 61.31,
    "weighted_avg_after": 63.84,
    "weighted_avg_delta": 2.53,
    "previously_passing_now_failing": []
  },
  "diff_summary": {"files_changed": 3, "insertions": 47, "deletions": 12},
  "evidence_refs": {
    "diff": "<run>/reports/<slice>/diff.patch",
    "breadth": "<run>/reports/<slice>/breadth.json",
    "scorecard": "<run>/reports/<slice>/scorecard.json"
  }
}
```

## Why orchestrator-in-loop instead of state machine

The legacy `obsessive_loop.py` picks the lowest-scoring category as the next
objective via `min(category.score)`. That misses:

- Cross-cutting dependencies (security must be tightened before observability
  instrumentation that ships secrets to logs).
- Existing-primitive reuse (the right fix may be wiring an existing logger
  module instead of adding a new one).
- Adapting the plan based on what's been learned (slice 3's outcome may
  invalidate slice 5's premise).

Those judgments live in the LLM. The Python is plumbing.

## Failure modes + responses

| Mode | Detection | Orchestrator response |
|---|---|---|
| Worker can't satisfy spec | goose_dispatch returns `outcome: fail` after retries | Reject; either replan with smaller scope or block the slice |
| Verify detects test cheat | goose_dispatch returns `outcome: gate_cheat_suspected` + flags | Reject; respec with explicit non-cheat constraint |
| Rubric regression elsewhere | report.rubric_delta.previously_passing_now_failing > 0 | Reject; revert the worktree to base_sha + iterate |
| Worker writes outside scope | files_changed extends beyond do_not_touch | Reject; restrict --allowed-paths in next dispatch |
| Slice unblocked by no other | summary shows blocked w/ no path to unblock | Mark abandoned; document in summary |

## Cost discipline

- Default `worker_runtime: goose` so each slice runs on the Pro/Max ACP path.
- The orchestrator (Opus high-effort) is the expensive one. Don't dispatch
  goose blindly — invest 1-2 minutes of orchestrator thinking per slice
  before the dispatch happens.
- `obsessive_slice_verify.sh` failing fast saves a goose iteration; iterate
  on the spec, not on the worker.
