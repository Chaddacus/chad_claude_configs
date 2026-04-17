# AutoConfig Research Program

## Current Phase: 1 — Model Assignment Sweep

### Objective
Determine the optimal model assignment for each agent role across each route.
The hypothesis is that not every agent needs opus — some roles (explorer, validator)
may perform equally well with faster/cheaper models.

### Key Questions
1. Is sonnet-worker 90% as good as opus-worker for R2 tasks?
2. Is haiku-explorer sufficient for R3 exploration?
3. Does opus-reviewer outperform sonnet-reviewer on security reviews (R4)?
4. What's the minimum model tier for R1 coordinator?

### Sweep Strategy
For each (agent_role, route) pair, try each model tier.
Start with roles that have the largest speed/quality tradeoff potential:
1. Explorer (currently haiku for R3/R4 — validate this is correct)
2. Validator (currently haiku for R3/R4 — validate)
3. Worker (currently opus for R3, sonnet for R2 — test haiku for R2)
4. Coordinator (currently sonnet for R1/R2 — test haiku for R1)
5. Reviewer (currently opus — test if sonnet is sufficient for R2)

### Success Criteria
- Composite score ≥ baseline after sweep
- Identify at least 2 model downgrade opportunities without quality loss
- Establish model tier recommendations per agent-route combination

---

## Phase 2: Effort Level Tuning

### Objective
Determine optimal effort levels. Higher effort = more reasoning tokens = slower but potentially better.

### Key Questions
1. Does high-effort planner produce better plans than medium?
2. Can worker use low effort for simple R2 tasks?
3. Is high-effort reviewer necessary for R4 security reviews?

### Sweep Strategy
For each (agent_role, route) pair, try each effort level: low, medium, high.
Focus on routes where effort has the most impact: R3 (complex) and R2 (speed-sensitive).

---

## Phase 3: Swarm Topology

### Objective
Optimize the swarm structure — lane caps, swarm caps, dispatch order, parallelism policy.

### Key Questions
1. Should R3 have 3 or 4 swarm cap?
2. Is validator-first dispatch order optimal, or should explorer go first?
3. Would aggressive_parallel outperform bounded_parallel for R3?
4. Should R4 swarm cap increase from 2 to 3?

### Sweep Strategy
Mutate one topology knob at a time:
- Lane caps: ±1 for each role in R3 and R4
- Swarm caps: ±1 for R3 and R4
- Dispatch order: test all reasonable permutations
- Parallelism policy: try each policy for R3

---

## Phase 4: Agent-Directed Discovery

### Objective
Let Claude propose config mutations based on experiment history.
Feed the top-10 and bottom-10 experiments, ask for a hypothesis.

### Strategy
Use a separate `claude -p` call to analyze experiment history and propose mutations.
Any non-immutable knob is fair game. This is where surprising improvements might emerge
from combinations or knobs we didn't think to sweep.

---

## Phase 5: Compound Optimization

### Objective
Test combinations of the top individual improvements.
An improvement that helps on its own might conflict with another.

### Strategy
1. Identify top-5 individual improvements from Phases 1-4
2. Test pairwise combinations
3. Test winning pairs as 3-way and 4-way combinations
4. Final tournament: best compound config vs current baseline

### Validation
3 runs per benchmark, take median score. This phase requires the highest confidence
because compound changes are harder to attribute.

---

## Future Phases (not yet active)

### Phase 6-8: Behavioral Optimization
Modify agent definition prose, skill workflows, planning-gate parameters.
Scored by rubric-judged quality via a separate judge model.

### Phase 9+: Meta-Optimization
Evolve benchmarks, evaluation rubrics, and this research plan itself.
Correlation tracking with real-world build outcomes.
