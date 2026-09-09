# Audit Fix Loop — Visual Flowchart

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
  ║   → Store final scores in omni-mem   ║
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
  ║ Save to omni-mem:                    ║
  ║   mcp__omni-mem__save_memory({       ║
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
  ║     })                               ║
  ║   })                                 ║
  ║                                      ║
  ║ Display round summary:              ║
  ║   Round N Summary                    ║
  ║   Before  → After                    ║
  ║   Avg: 2.8 → 3.4 (+0.6)            ║
  ║   Min: 1   → 2   (+1)              ║
  ║   Improved: Security: 2 → 4 (+2)   ║
  ║   E2E: 113/125 passed               ║
  ║   Remaining to enterprise-ready:     ║
  ║   - Type Safety: 3 (need 4)        ║
  ║                                      ║
  ╚═══════════════════════════════════════╝
            │
       GOTO LOOP
```
