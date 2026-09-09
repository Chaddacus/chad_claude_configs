# Canonical Workflow Phases

This is the authoritative phase sequence for all non-trivial work. Every skill,
hook, and runtime component references this document for where it sits in the
pipeline. The auto_runtime (`auto_runtime.py`) tracks phase transitions; `/govern`
enforces them.

## The 9 Phases

```
PROMPT → CLASSIFY → RESEARCH → BLUEPRINT → PLAN → AUDIT → IMPLEMENT → TEST → VALIDATE
  1         2          3           4          5       6        7          8       9
```

### Phase 1: PROMPT

User sends the task. Raw input, no processing yet.

### Phase 2: CLASSIFY

Route assignment sets the ceremony level for everything downstream.

- **Mechanism:** `classify_prompt.py` via UserPromptSubmit hook
- **Output:** route (R1–R5), risk class, execution shape
- **Short-circuits:**
  - R1 → skip to Phase 7 (answer directly, no track)
  - R5 → clarify ambiguity, then re-classify and re-enter at the resolved route

### Phase 3: RESEARCH

Gather context before designing. Two sources:

1. **omni-mem retrieval** — prior decisions, workspace preferences, known
   constraints, durable observations (`search`, `build_context`)
2. **External grounding** — if the task touches unfamiliar territory, invoke
   `/deep-research` or wigolo for evidence-bound sourcing

Also read: project CLAUDE.md, relevant READMEs, recent git history of affected areas.

- **Output:** `research_context` — memory hits, external sources, repo state
- **Gate:** if memory is weak or low-confidence, surface that weakness — do not
  pretend the context is strong

### Phase 4: BLUEPRINT

Map the full topology of the work. This phase answers **what** and **where** —
not how.

1. **Blast radius** — every file, service, migration, API surface, and config
   that the task touches or could affect
2. **Dependency graph** — what blocks what (file A imports from file B, migration
   must run before model changes, etc.)
3. **Parallelism map** — which pieces are independent and can fan out to
   subagents vs. which must be sequential
4. **Current state** — for each surface in the blast radius: does it exist? does
   it have tests? is it lint-clean? any recent churn?

- **Output:** `blueprint` artifact — topology, dependencies, parallelism
  boundaries, blast radius inventory
- **Key discipline:** this is where you catch "wait, this also touches the auth
  middleware" — before you've committed to a plan. Scope surprises found here
  cost nothing; scope surprises found in Phase 7 cost a replan.

### Phase 5: PLAN

Design the execution. This phase answers **how** and **when**.

1. **Solution ladder** — L1 (patch) / L2 (abstraction) / L3 (operating surface),
   with `why_not_lower` and `why_not_higher` justification
2. **Packet decomposition** — slice the blueprint's dependency graph into
   ordered work packets, each with:
   - owned files (from the blueprint)
   - acceptance criteria (falsifiable — "exit 0" not "works")
   - estimated effort
3. **Topological order** — dependency-respecting execution sequence from the
   blueprint's graph
4. **Sprint contract** — concise criteria list (<=8 bullets) sent to reviewer
   for explicit ack. No execution without ack (R3/R4).

- **Output:** `plan` artifact — packets, criteria, schedule, sprint contract
- **Gate (R3/R4):** reviewer ack on sprint contract before proceeding

### Phase 6: AUDIT

Pre-implementation baseline of everything you're about to touch. Purpose: so
you can prove you didn't break what was working.

1. **Test coverage** — run coverage for files in the blast radius, record which
   lines/branches are currently covered
2. **Lint/typecheck state** — run linters and typecheck, record current pass/fail
3. **Known fragility** — check git log for recent failures, flaky test history,
   open issues against touched files
4. **E2E baseline** — if E2E suite exists, run it and record pass count

- **Output:** `baseline_audit` — coverage numbers, health status, known issues
- **Key discipline:** this is a snapshot, not a fix pass. Do not fix pre-existing
  problems here — record them so Phase 8 can distinguish your regressions from
  inherited debt.

### Phase 7: IMPLEMENT

Execute the plan's packets in topological order.

1. Per slice: **implement → test the changed code → fix failures**
2. Parallel fan-out where the blueprint's parallelism map allows
3. Commit at every green slice boundary on `codex/` branch
4. If a slice changes a directory's behavior, update that directory's README
5. Do not stop between slices. Do not report progress.

- **Output:** implementation evidence — diffs, per-slice test output,
  criterion-by-criterion mapping
- **Mechanisms:** `/drive`, `/build`, direct implementation, subagent dispatch

### Phase 8: TEST

Full verification against the Phase 6 baseline.

1. **Full suite** — run the complete test suite, not just per-slice checks
2. **Regression check** — compare against baseline_audit: no previously-passing
   tests now failing
3. **New coverage** — verify new code has tests, check coverage delta
4. **Typecheck + lint** — must be clean (or no worse than baseline)

- **Output:** `test_evidence` — full results, delta from baseline, regression
  report
- **Gate:** regression = fix or revert before proceeding. New test failures
  introduced by your changes are your problem, not pre-existing debt.

### Phase 9: VALIDATE

Confirm the work meets the contract and record the closure.

1. **validate_impl.py** — plan vs. implementation evidence (R3/R4)
2. **finalize_gate.py** — must return `ok=true` (R3/R4)
3. **Self-audit** — re-read the request, verify every requirement addressed,
   name gaps
4. **Expert review** — correctness, regressions, failure modes, security
5. **Stop-gate L2** — file completion record via `claim_complete.py`
6. **what-would-chad-do** — is there one more bounded, high-leverage step?
7. **Memory persistence** — save durable decisions to omni-mem

- **Output:** `validation_record` — gate results, completion evidence, closure
  type (strong / weak / blocked)

---

## Route × Phase Matrix

Not every route runs every phase. The classify step sets the ceremony level:

```
Phase            R1    R2    R3    R4    R5
─────────────────────────────────────────────
1. Prompt        ✓     ✓     ✓     ✓     ✓
2. Classify      ✓     ✓     ✓     ✓     ✓
3. Research      ·    opt     ✓     ✓     ✓
4. Blueprint     ·     ·     ✓     ✓     ·
5. Plan          ·     ·     ✓     ✓     ·
6. Audit         ·    opt     ✓     ✓     ·
7. Implement     ✓*    ✓     ✓     ✓     ·
8. Test          ·     ✓     ✓     ✓     ·
9. Validate      ·    lite    ✓     ✓     ·

✓  = required
opt = recommended, use judgment
lite = lightweight (no formal gates, just verify + self-audit)
·  = skip
✓* = R1 "implement" = answer the question directly

R5: clarify → re-classify → re-enter at resolved route
```

### R2 shortcut

R2 runs a compressed sequence: research (if useful) → implement → test →
lightweight validate. No blueprint, no formal plan, no pre-audit. The phase
names still apply — a quick mental pass through "what am I touching, what
could break" is the R2 blueprint; it's just not a written artifact.

### R3 vs R4 differences

Both run all 9 phases. R4 adds:
- Reviewer is co-primary from Phase 5 (joins at plan, not just at closure)
- Tighter swarm caps (2 vs 4)
- Extra reviewer barrier at `adaptation_generated_packets`
- All worker packets require explicit reviewer sign-off

---

## Phase Artifacts

Each phase produces a named artifact that downstream phases consume:

```
Phase 2 → route_decision        (route, risk, shape)
Phase 3 → research_context      (memory hits, sources, repo state)
Phase 4 → blueprint             (topology, deps, parallelism, blast radius)
Phase 5 → plan                  (packets, criteria, schedule, contract)
Phase 6 → baseline_audit        (coverage, health, known issues)
Phase 7 → impl_evidence         (diffs, test output, criterion map)
Phase 8 → test_evidence         (full results, baseline delta, regressions)
Phase 9 → validation_record     (gate results, closure type, evidence)
```

The auto_runtime track persists these under
`~/.claude/state/autonomy/{track_id}/`.

---

## Skill × Phase Mapping

Which skills/tools own which phases:

| Phase | Primary owner | Supporting |
|---|---|---|
| 2. Classify | `classify_prompt.py` (hook) | `classify_route.py` (govern) |
| 3. Research | omni-mem retrieval | `/deep-research`, wigolo |
| 4. Blueprint | `/govern` (new: Phase 2 split) | explorer subagent |
| 5. Plan | `/planning-gate` | `/govern` |
| 6. Audit | `/audit --category` (targeted) | sentinel `/validate` |
| 7. Implement | `/drive`, `/build`, direct | worker subagents |
| 8. Test | execution loop | validator subagent |
| 9. Validate | `validate_impl.py`, `finalize_gate.py` | `claim_complete.py` |

---

## Ownership

This document is the canonical definition of the workflow phase sequence.
- **Canonical owner:** `~/.claude/standards/WORKFLOW_PHASES.md`
- **Consumers:** `/govern` (enforces), `auto_runtime.py` (tracks), CLAUDE.md
  (references), all skills (position themselves)
- **Changes:** update this document first, then update consumers
