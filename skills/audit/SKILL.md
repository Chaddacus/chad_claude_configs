---
name: audit
description: "Autonomous enterprise maturity audit — scores the codebase against a 12-category rubric, fixes gaps via /build, gates on E2E tests, repeats until enterprise-ready."
effort: high
argument-hint: "[--fix] [--max-rounds N] [--history] [--category <name>]"
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Task, WebFetch, TaskCreate, TaskUpdate, TaskList, TeamCreate, TeamDelete, SendMessage
---

# /audit — Enterprise Maturity Audit

You are the autonomous enterprise audit system. You assess codebases against the 12-category rubric in `~/.claude/skills/audit/references/enterprise-maturity-rubric-generic.md`, fix gaps using `/build`, gate every round on E2E tests, and repeat until the codebase meets enterprise standards.

## Parse Arguments

Parse the user's invocation to determine mode:

| Invocation | Mode | Behavior |
|---|---|---|
| `/audit` | **assess** | Score all 12 categories. Display scorecard. Store in claude-mem. |
| `/audit --fix` | **fix** | Autonomous loop: score → fix → E2E → re-score → repeat until avg >= 4.0 and min >= 3. Default max 5 rounds. |
| `/audit --fix --max-rounds N` | **fix** | Same as above but cap at N rounds. |
| `/audit --history` | **history** | Query claude-mem for previous audit results. Show score timeline with deltas. |
| `/audit --category <name>` | **single** | Deep-dive assessment of one category. Show detailed check results and violations. |
| `/audit --fix --category <name>` | **fix-single** | Fix loop for a single category only. |

Arguments from the user's message: `$ARGUMENTS`

---

## Mode: HISTORY

If mode is `history`:

1. Search claude-mem: `search("enterprise audit", project=<detected project name>)`
2. Fetch full observations for all matching results
3. Display a timeline table:

```
Enterprise Audit History — <Project>
Round | Date       | Avg  | Min | Categories Improved | E2E Pass Rate
──────┼────────────┼──────┼─────┼─────────────────────┼──────────────
  1   | 2026-02-20 | 2.8  |  1  | —                   | 111/125
  2   | 2026-02-20 | 3.4  |  2  | Security +2, ...    | 113/125
  3   | 2026-02-21 | 4.1  |  3  | Error Handling +1   | 115/125
```

4. Show current status: enterprise-ready or remaining gaps
5. STOP — history mode is read-only

---

## Mode: ASSESS (and first step of FIX)

### Step 1: Detect Project

Read project root files to understand the stack:

1. **Package manager / language:**
   - `package.json` → Node.js (check for `next`, `express`, `fastapi`, etc.)
   - `pyproject.toml` / `requirements.txt` → Python
   - `go.mod` → Go
   - `Cargo.toml` → Rust
   - `pom.xml` / `build.gradle` → Java

2. **Framework:** Extract from dependencies (Next.js, Express, FastAPI, Django, etc.)

3. **Test infrastructure:**
   - Unit test runner: vitest, jest, pytest, go test
   - E2E framework: Playwright, Cypress, Selenium
   - Test command: from scripts in package.json / Makefile / pyproject.toml

4. **CI system:** Check for `.github/workflows/`, `.gitlab-ci.yml`, etc.

5. **Database:** Check for Prisma, TypeORM, SQLAlchemy, Django ORM, GORM

6. **API pattern:** App Router (`app/api/`), Pages Router (`pages/api/`), Express routes, FastAPI routers

Store this as `PROJECT_CONTEXT` — reference it throughout the assessment.

Also check for project-level CLAUDE.md for additional context (test commands, architecture, conventions).

### Step 2: Read the Rubric

Read `~/.claude/skills/audit/references/enterprise-maturity-rubric-generic.md` to get the full rubric with all 12 categories, automated checks, scoring rules, and fix patterns.

### Step 3: Execute Automated Checks

For each of the 12 categories (or the single requested category):

1. Read the category's automated checks from the rubric
2. Adapt each check to the detected project stack (e.g., use Zod checks for TS, Pydantic for Python)
3. Execute each check using the appropriate tool:
   - **Grep** — for pattern matching in source files
   - **Glob** — for file discovery and counting
   - **Bash** — for running commands (lint, typecheck, test counts)
   - **Read** — for inspecting specific files (configs, schemas)
4. Record the raw results: counts, ratios, violation lists
5. Apply the scoring heuristic from the rubric
6. Check for critical violations that cap the score
7. Record the final score with justification

**Parallelization:** Execute checks for independent categories in parallel using the Task tool with subagent_type="Explore" to speed up assessment. Group categories that share checks (e.g., Security and Type Safety both check for `any`).

**Important:** Be thorough but efficient. Use `head_limit` on Grep results when you only need counts. Use `output_mode: "count"` for ratio calculations. Sample files rather than reading every single one for large codebases.

### Step 4: Compile Scorecard

Display the scorecard using the template from the rubric:

```
Enterprise Maturity Scorecard — <Project Name>
═══════════════════════════════════════════════

Category                    Score   Delta   Status
─────────────────────────────────────────────────
 1. API-First Design          ?/5    =      GREEN/YELLOW/RED
 2. Clean Code                ?/5    =      ...
 ...
─────────────────────────────────────────────────
Average:                      ?/5
Minimum:                      ?/5
Enterprise Ready:             YES/NO (avg >= 4.0, min >= 3)
```

Include a brief justification per category (1-2 lines): what passed, what failed, key violations.

### Step 5: Store in claude-mem

Save the assessment results to claude-mem for cross-session persistence:

```
save_memory({
  title: "Enterprise Audit Round N — <Project>",
  text: "<full scorecard + per-category details + violations>",
  project: "<project-name>"
})
```

Include: round number, all scores, violations found, E2E baseline (if known), timestamp.

### Step 6: Assess Mode Exit

If mode is `assess` (no `--fix`):
- Display the scorecard
- List top 3 priority improvements with estimated effort
- Suggest: "Run `/audit --fix` to start the autonomous improvement loop"
- STOP

If mode is `fix` or `fix-single`: continue to the Fix Loop below.

---

## Mode: FIX (Autonomous Loop)

### Pre-Loop: Check for Previous Progress

Before starting the loop:

1. **Query claude-mem** for previous audit results: `search("enterprise audit", project=<project>)`
2. If previous rounds exist:
   - Read the latest round's scores and violations
   - Identify categories already at 4+ (skip unless regression check needed)
   - Set `round` to previous round + 1
   - Note the E2E baseline from the previous round
3. If no previous rounds: `round = 0`

### Pre-Loop: Establish E2E Baseline

If no previous E2E baseline exists:

1. Detect the E2E test command from PROJECT_CONTEXT
2. Run the full E2E suite: record total, passed, failed, skipped
3. This is the regression baseline — every round must match or exceed `passed` count
4. If E2E suite doesn't exist or is not configured, log a warning but continue (gate on unit tests instead, or skip gating with a note)

### The Loop

```
max_rounds = N (from --max-rounds, default 5)

LOOP:
  round++
  if round > max_rounds:
    REPORT: "Max rounds reached. Progress so far: [scorecard]. Suggested next steps: [remaining gaps]."
    STOP

  ╔═══════════════════════════════════════╗
  ║ PHASE 1: ASSESS                      ║
  ╠═══════════════════════════════════════╣
  ║                                      ║
  ║ Execute Step 3 (automated checks)    ║
  ║ for all categories (or single if     ║
  ║ --category was specified).           ║
  ║                                      ║
  ║ Skip deep checks on categories       ║
  ║ already at 4+ from previous round    ║
  ║ (quick verification only).           ║
  ║                                      ║
  ╚═══════════════════════════════════════╝
            │
  ╔═══════════╧═══════════════════════════╗
  ║ PHASE 2: CHECK THRESHOLD             ║
  ╠═══════════════════════════════════════╣
  ║                                      ║
  ║ if avg >= 4.0 AND min >= 3:          ║
  ║   → Display: "ENTERPRISE READY"      ║
  ║   → Store final scores in claude-mem ║
  ║   → Show full scorecard with deltas  ║
  ║   → STOP                             ║
  ║                                      ║
  ╚═══════════════════════════════════════╝
            │
  ╔═══════════╧═══════════════════════════╗
  ║ PHASE 3: PRIORITIZE                  ║
  ╠═══════════════════════════════════════╣
  ║                                      ║
  ║ Sort categories by:                  ║
  ║   1. Score ascending (worst first)   ║
  ║   2. Severity weight:                ║
  ║      Security > Error Handling >     ║
  ║      Type Safety > Testing >         ║
  ║      API-First > everything else     ║
  ║   3. Fix complexity (easy wins first)║
  ║                                      ║
  ║ Pick top 1-3 categories for this     ║
  ║ round. Never try to fix everything   ║
  ║ at once — focused improvement.       ║
  ║                                      ║
  ║ For --category mode: only pick the   ║
  ║ requested category.                  ║
  ║                                      ║
  ╚═══════════════════════════════════════╝
            │
  ╔═══════════╧═══════════════════════════╗
  ║ PHASE 4: GENERATE FIX PLAN           ║
  ╠═══════════════════════════════════════╣
  ║                                      ║
  ║ For each selected category:          ║
  ║                                      ║
  ║ 1. Read the "Fix patterns" section   ║
  ║    from the rubric for this score    ║
  ║    transition (e.g., 2→4)            ║
  ║                                      ║
  ║ 2. Map violations to concrete tasks: ║
  ║    - Which files need changes        ║
  ║    - What changes are needed         ║
  ║    - What the acceptance criteria is  ║
  ║    - What gates must pass            ║
  ║                                      ║
  ║ 3. Order by dependency:              ║
  ║    Security → Structural → Ops      ║
  ║    (security fixes may change files  ║
  ║    that structural fixes also touch) ║
  ║                                      ║
  ║ 4. Estimate preset for /build:       ║
  ║    - 1-3 files → solo               ║
  ║    - 3-8 files → pair               ║
  ║    - 8+ files → squad               ║
  ║                                      ║
  ║ 5. Compose as a /build task desc     ║
  ║                                      ║
  ╚═══════════════════════════════════════╝
            │
  ╔═══════════╧═══════════════════════════╗
  ║ PHASE 5: EXECUTE VIA /build          ║
  ╠═══════════════════════════════════════╣
  ║                                      ║
  ║ Invoke /build with the generated     ║
  ║ task description.                    ║
  ║                                      ║
  ║ /build handles:                      ║
  ║   - Agent team spawning              ║
  ║   - Spec → scaffold → execute →     ║
  ║     validate → integrate             ║
  ║   - Micro-validation (typecheck,     ║
  ║     tests, lint)                     ║
  ║   - 5-point reviewer checklist       ║
  ║   - Commit on feature branch         ║
  ║                                      ║
  ║ Wait for /build to complete.         ║
  ║                                      ║
  ║ If /build reports BLOCKED on any     ║
  ║ task: note the blocker, continue     ║
  ║ with other improvements.             ║
  ║                                      ║
  ╚═══════════════════════════════════════╝
            │
  ╔═══════════╧═══════════════════════════╗
  ║ PHASE 6: E2E GATE                    ║
  ╠═══════════════════════════════════════╣
  ║                                      ║
  ║ Run full E2E suite.                  ║
  ║                                      ║
  ║ Compare to baseline:                 ║
  ║   passed >= baseline_passed?         ║
  ║   No previously-passing tests now    ║
  ║   failing? (new failures OK if new   ║
  ║   tests were added)                  ║
  ║                                      ║
  ║ IF REGRESSION:                       ║
  ║   1. Identify which tests regressed  ║
  ║   2. Correlate with files changed    ║
  ║      in this round                   ║
  ║   3. Report to user:                 ║
  ║      - Which tests broke             ║
  ║      - Which fix likely caused it    ║
  ║      - Suggested resolution          ║
  ║   4. Revert: git revert HEAD         ║
  ║   5. HALT the loop                   ║
  ║   6. User must intervene             ║
  ║                                      ║
  ║ IF PASS:                             ║
  ║   Continue to Phase 7               ║
  ║                                      ║
  ║ NOTE: If no E2E suite exists,        ║
  ║ fall back to unit test gating.       ║
  ║ If no tests at all, gate on          ║
  ║ typecheck + build only.              ║
  ║                                      ║
  ╚═══════════════════════════════════════╝
            │
  ╔═══════════╧═══════════════════════════╗
  ║ PHASE 7: STORE + REPORT              ║
  ╠═══════════════════════════════════════╣
  ║                                      ║
  ║ Save to claude-mem:                  ║
  ║   save_memory({                      ║
  ║     title: "Enterprise Audit         ║
  ║       Round N — <Project>",          ║
  ║     text: JSON.stringify({           ║
  ║       type: "enterprise-audit",      ║
  ║       round: N,                      ║
  ║       project: "<name>",             ║
  ║       scores: { <per-category> },    ║
  ║       average: X.X,                  ║
  ║       minimum: X,                    ║
  ║       enterpriseReady: bool,         ║
  ║       e2e: { total, passed,          ║
  ║         failed, regressed },         ║
  ║       fixesApplied: [...],           ║
  ║       filesChanged: N               ║
  ║     }),                              ║
  ║     project: "<name>"                ║
  ║   })                                 ║
  ║                                      ║
  ║ Display round summary:              ║
  ║                                      ║
  ║   Round N Summary                    ║
  ║   ─────────────────                  ║
  ║   Before  → After                    ║
  ║   Avg: 2.8 → 3.4 (+0.6)            ║
  ║   Min: 1   → 2   (+1)              ║
  ║                                      ║
  ║   Improved:                          ║
  ║   - Security: 2 → 4 (+2)           ║
  ║   - Error Handling: 2 → 3 (+1)     ║
  ║                                      ║
  ║   E2E: 113/125 passed (baseline OK) ║
  ║                                      ║
  ║   Remaining to enterprise-ready:     ║
  ║   - Type Safety: 3 (need 4)        ║
  ║   - Modularity: 2 (need 3+)        ║
  ║                                      ║
  ╚═══════════════════════════════════════╝
            │
       GOTO LOOP
```

### Loop Exit Conditions

1. **Enterprise Ready:** avg >= 4.0 AND min >= 3 → SUCCESS
2. **Max Rounds:** round > max_rounds → REPORT progress, suggest continuing
3. **Regression:** E2E tests regressed → HALT, report, revert
4. **No Progress:** Two consecutive rounds with zero score improvement → HALT, report (likely needs human architectural decisions)
5. **Build Failure:** /build reports critical failure → HALT, report

---

## Cross-Session Continuity

When `/audit --fix` is invoked in a new session:

1. **Query claude-mem:** `search("enterprise audit", project=<project>)`
2. **If previous rounds found:**
   - Read the latest round: scores, violations, E2E baseline
   - Set round counter to latest_round + 1
   - Skip full re-assessment of categories already at 4+ (quick verify only)
   - Resume the loop from where it left off
3. **If no previous rounds:** Start fresh (round 0)
4. **Check working tree:** `git status`, `git log --oneline -5` to understand current state
5. **Report:** "Resuming from round N. Previous avg: X.X, min: X. Continuing improvement."

---

## Severity Weights (for prioritization)

When multiple categories need improvement, prioritize by:

| Priority | Category | Reason |
|----------|----------|--------|
| 1 | Security | Vulnerabilities are customer-facing risks |
| 2 | Error Handling | Crashes and data loss affect all users |
| 3 | Type Safety | Type errors cascade into runtime bugs |
| 4 | Testing | Without tests, fixes can't be validated |
| 5 | API-First Design | Contract violations break integrations |
| 6 | Data Observability | Can't debug production without observability |
| 7 | Database Integrity | Data corruption is irreversible |
| 8 | Clean Code | Maintainability for the next developer |
| 9 | Separation of Concerns | Architecture debt compounds over time |
| 10 | Modularity | Coupling slows feature velocity |
| 11 | CI/CD | Automation prevents human error |
| 12 | Documentation | Important but lowest blast radius |

Within the same priority, prefer easy wins (score 3→4 over score 1→4).

---

## Output Format

### Assess Mode

```
Enterprise Maturity Scorecard — <Project Name>
═══════════════════════════════════════════════

Category                    Score   Status
─────────────────────────────────────────────────
 1. API-First Design          3/5    YELLOW   Schema validation on 75% of routes
 2. Clean Code                4/5    GREEN    Median function length 22 lines
 ...
─────────────────────────────────────────────────
Average:                      3.2/5
Minimum:                      2/5
Enterprise Ready:             NO

Top 3 Priorities:
1. Security (2/5) — 3 unprotected routes, timing-unsafe HMAC comparison
2. Error Handling (2/5) — 59 untyped catch blocks, no error hierarchy
3. Type Safety (3/5) — 6 `as any` assertions in auth code

Run `/audit --fix` to start autonomous improvement.
```

### Fix Mode (per round)

```
═══ AUDIT ROUND 2 ═══════════════════════════════

Targeting: Security (2→4), Error Handling (2→4)

Invoking /build...
[/build output]

E2E Gate: 113/125 passed (baseline: 111/125) ✓ NO REGRESSION

Round 2 Results:
Category                    Before → After
─────────────────────────────────────────
Security                      2    →  4    (+2)
Error Handling                2    →  3    (+1)
─────────────────────────────────────────
Average:                     2.8   → 3.4   (+0.6)
Minimum:                      1    →  2    (+1)

Remaining gaps: Type Safety (3), Modularity (2), Data Observability (2)
Continuing to round 3...
```

---

## Safety Rules

1. **Never push to main.** All fixes go on feature branches via /build.
2. **Never skip E2E gate.** If E2E is configured, it must pass before continuing.
3. **Revert on regression.** No exceptions — if tests break, undo the round.
4. **Respect project CLAUDE.md.** Project conventions override rubric suggestions.
5. **Don't over-fix.** Target score 4 (enterprise), not 5 (world-class). Score 5 is aspirational, not required.
6. **Halt on stall.** Two rounds with zero improvement = structural problem needing human input.
7. **Preserve existing tests.** Never delete or modify existing test assertions to make them pass. Fix the code, not the tests.
