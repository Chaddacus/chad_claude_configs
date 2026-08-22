# Orchestration Playbook — supervisor mechanics for any orchestrating session

Extracted from the retired `chad-twin` agent (archived 2026-08-22 at `agents-archive/chad-twin.md`)
so the doctrine survives agent-independently. Applies to WHATEVER session is orchestrating —
`chad-work`, `chad-personal`, `claude`, or a repo-scoped agent. The Standards in `~/.claude/rules/`
are the policy; this file is the operating manual for executing Standard 3 with subagents.

## When supervisor mode fires

Only when the task genuinely decomposes into parallel, low-conflict slices and worker spawn pays for
itself. Do not parallelize slices that touch shared state or the same files. Multi-agent costs ~15x
single-agent tokens — every spawn must earn it. Effort scaling is the hub's job: 1 agent for a
simple lookup; 2–4 for comparisons or medium decomposition; wide fan-out only for read-only sweeps.

## Dispatch envelope

Every Agent dispatch carries four fields (vague dispatches are the root cause of duplicate work and gaps):

1. **Objective** — what done looks like, one sentence.
2. **Output format** — the exact shape of the artifact to return.
3. **Tool guidance** — which tools/sources to use, which to avoid.
4. **Task boundaries** — what is explicitly out of scope for this dispatch.

Every worker return carries the handoff signal: **uphill** (unsolved unknowns remain — named) or
**downhill** (all unknowns retired; pure execution). Percent-complete claims are not status
(SPEC Standard 3 §6.5).

## The supervisor loop

1. Memory retrieval (recommended for repeat task types; required on R3/R4 per global policy).
2. Decompose: problems → slices → dependency DAG, with an appetite per the plan-change contract.
3. Spawn workers for runnable slices (background, isolated worktrees). Dispatch parallel slices
   together; review each as it returns — don't batch reviews, don't wait serially.
4. Review each diff: **Accept** (merge, mark done, dispatch next) · **Reject** (specific, blunt
   feedback; worker iterates) · **Blocked** (notify with context, pause that slice, continue others).
5. **2-attempt rule:** 2 attempts max per worker per slice. A worker that fails twice means the
   slice is too ambiguous or the approach is wrong — re-decompose or change approach, never retry
   harder. A slice reporting uphill across two consecutive reviews is the same signal.
6. Final verification: full test suite + typecheck across everything that changed.

**On a 2-attempt (or any replan) trigger — CR-INV-009, enforced:** before re-dispatching, write the
sentinel `/tmp/claude-replan-pending-<session>.json` and record the pivot as a structured
`replan-*` journal entry per `standards/REPLAN_DECISION_PROTOCOL.md`. The Stop hook
(`bin/replan_evidence_check.py --strict`) blocks session close while the sentinel exists without a
matching journal entry.

If a session resets mid-supervision: read the TaskList for what's done and the worktree branches
for in-progress work; pick up where it left off. Don't restart completed slices.

## Coding-team pipeline (stage compositions)

When work warrants the full team, run six stages in order; fan out only inside a stage, never
across them. Only the hub spawns — subagent nesting is blocked.

| Stage | Agents | Fan-out |
|---|---|---|
| 1 Dissect / Research | `explorer`, `deep-research` | Wide OK — read-only |
| 2 Plan | `planner` | None |
| 3 Audit | `auditor` | Wide OK — read-only; loops back to Plan on findings |
| 4 Implement | `worker`(s), worktree isolation | File-disjoint slices only; otherwise sequential |
| 5 Test | `implementation-checker` → `validator` → `test-strategist` (on gaps) | Sequential gates |
| 6 Validate | `reviewer` + `typescript-reviewer`/`python-reviewer` | Loop back to Implement; 2-attempt cap, then re-decompose |

Refactor work runs the same six stages with Audit load-bearing; the procedure and its gates are
owned by `~/.claude/skills/refactor/SKILL.md`. Mechanical transforms across more than 3 files use
codemods (`ast-grep`/`jscodeshift`), not hand edits — a hand-edited mechanical transform at that
size is a reject.

## Scope gate (operational form of the Engineering Constitution)

A change exceeding 500 LOC or 3 files requires a one-sentence justification before implementing
(CR-INV-010). Anti-overengineering is a gate, not an aspiration: no new service/persistence/
orchestration surface unless an existing primitive provably cannot satisfy the requirement.
