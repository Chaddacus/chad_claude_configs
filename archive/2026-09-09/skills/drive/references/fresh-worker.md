# /drive --fresh-worker — delegate slices to fresh claude workers (CP6)

Loaded on demand by `skills/drive/SKILL.md` Phase 2. Read this when the CP6
backend is selected: `--fresh-worker`, `--heavy`, or a plan of >=2 slices.
Inert otherwise.

When `--fresh-worker` is set, each slice runs in a FRESH `claude --print` worker in
an isolated git worktree, driven by the CP6 outer-loop driver
(`~/.claude/bin/outer_loop_driver.py`). Claude stays supervisor: plan slices + verify
commands; the driver spawns/respawns workers (CP7 retry policy) and records a slice
`accepted` **only** on passing verifier evidence — the supervisor, not the worker,
decides done. This fixes context accumulation: every worker gets a clean window sized
to one slice, so a derailed/context-blown worker is cheaply discarded and respawned.

**Standing-capability gate:** workers run with `--permission-mode acceptEdits`
(least-privilege; the settings deny-list still applies). The auto-mode classifier
hard-gates self-enabling this loop — enable it by adding Bash allow-rules for
`python3 ~/.claude/bin/outer_loop_driver.py` and `claude --print`, or run out of auto
mode. This is a standing autonomous editing mechanism; enable deliberately.

**Preflight (fail fast):**
```bash
test -x ~/.claude/bin/outer_loop_driver.py
git -C "$PWD" diff --quiet && git -C "$PWD" diff --cached --quiet   # clean worktree required
```

**Slice discipline:** each slice needs a deterministic verify command. Either set
per-slice `slice_contract.verification_commands` in the track, or pass a project-wide
`--default-verify "<test cmd>"` used for any slice lacking one. A slice with neither
is blocked (fail-closed) rather than accepted unverified.

**Dispatch (drives the whole track to closure in one call):**
```bash
python3 ~/.claude/bin/outer_loop_driver.py \
  --track-id <track_id> --main-repo "$PWD" \
  --model sonnet --max-attempts 3 \
  --default-verify "<project test/verify command>"
```

**Outcome handling** (the driver prints a JSON summary; exit 0 iff OBJECTIVE_COMPLETE):
- `closure_state: OBJECTIVE_COMPLETE` → every slice accepted and ff-merged into main
  as a small revertable commit. **Then the MANDATORY post-track review** (see below)
  before the run may be declared done.
- a `blocked` result → read its `stage`/`reason`; a hard-stage failure or an exhausted
  slice is where the supervisor (Claude) takes it in-session or replans.
- CP7 events on stderr (`slice_attempt_failed`, `rate_limited`, `slice_accepted`)
  narrate retries.

**Post-track review (mandatory on OBJECTIVE_COMPLETE).** CP6 workers run agent-less
with only exit-0 acceptance — the review is where judgment-tier quality control
happens, once per track (cross-slice view, amortized cost):
1. The summary carries the review span: `base_sha` (HEAD before slice 1) and
   `final_sha` (HEAD after the last merge).
2. Dispatch the `reviewer` agent (two-stage) over `git diff <base_sha>..<final_sha>`
   in the target repo, with the track's objective as context.
3. Grounded findings → fix them (new slices via the driver, or in-session for small
   ones) and re-run the review on the fix delta. "No grounded findings" → done.
4. Do NOT declare the run complete, close the track summary to the user, or start
   dependent work before this pass. A skipped review is a false completion.

**Isolation guarantees:** worker worktrees live OUTSIDE the repo and prompts carry
only repo-relative paths; `worker_sandbox`'s HEAD-drift guard fails closed if a worker
touches main directly (so a leak is refused, never merged). Verified live 2026-07-15.

Files this flag uses: `~/.claude/bin/outer_loop_driver.py` (CP6), `slice_retry.py`
(CP7), `verifier_shim.py`, `slice_executor.py` (CP5) + `worker_sandbox.py`/
`static_gates.py`/`verifier.py`/`apply_rehearsal.py` (CP1–CP4).
