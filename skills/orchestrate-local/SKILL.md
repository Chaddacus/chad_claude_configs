---
name: orchestrate-local
description: Full autonomous development loop where Claude plans+reviews and a local goose worker (Qwen3-Coder-30B on LM Studio) executes slices. Use when the user wants hands-off end-to-end implementation on tasks bounded enough for a local model, with Claude retaining planning and verification authority. Cheaper than /drive because execution runs on the 4090, not on Anthropic API.
---

# /orchestrate-local — Claude plans, goose executes, Claude reviews

A two-tier autonomous loop: **you (Claude)** are the supervisor — plan slices, curate briefs, verify outcomes, escalate failures. **Goose + local Qwen3-Coder-30B** is the worker — writes code, runs tests, reports. The verify-script (not either model) is the truth signal.

## When to use this

Use `/orchestrate-local` instead of `/drive` when:
- Task decomposes into clearly bounded slices (file + spec + acceptance test per slice)
- Work is code-heavy, not judgment-heavy (writing functions, tests, boilerplate, small refactors)
- User is okay with ~Sonnet-quality workmanship on the slices themselves (supervisor catches major errors)
- Cost matters — this runs execution on the local 4090 at $0/token

Do NOT use this when:
- The task is exploratory ("figure out why X is broken") — needs Claude-level reasoning on the whole task
- The codebase is unfamiliar and slicing is premature — use `/drive --heavy` or ask first
- Slices can't be verified deterministically (no tests, no types, no lint, no acceptance criteria)
- The user asked for `/drive` specifically

## Prerequisites (assume true unless checking)

- LM Studio running on 192.168.1.9:1234 with `daily-heavy` model loaded at ≥32k context
- Goose CLI installed (`/opt/homebrew/bin/goose`)
- socat port-forward `localhost:1234 → 192.168.1.9:1234` active
- Dispatcher at `~/.claude/bin/goose_dispatch.py` exists
- `~/.goosehints` exists
- `~/.config/goose/skills/*.md` populated

If any of these seem absent, run `~/.claude/bin/orchestrate_preflight.sh` (if it exists) or check manually. Fail fast — do not try to proceed with a half-wired system.

## Workflow

### Phase 0 — Preflight

1. Quickly confirm the LM Studio endpoint is reachable:
   ```bash
   curl -s --max-time 3 http://localhost:1234/v1/models | python3 -c "import sys,json; d=json.load(sys.stdin); assert any(m['id']=='daily-heavy' for m in d['data'])"
   ```
   If this fails, stop and tell the user the local worker is down.
2. Confirm dispatcher exists: `test -x ~/.claude/bin/goose_dispatch.py`.
3. Initialize an auto_runtime track if the work is non-trivial:
   ```bash
   python3 ~/.claude/bin/auto_runtime.py init --task "<user goal>" --cwd "$PWD"
   ```
   Save the `track_id`. Slices below will be `update-node`'d on it.

### Phase 1 — Plan slices

Think hard about the user's goal. Decompose into **bounded slices**, each:
- Touches ≤3 files
- Has a clear **spec** (what to build / change, in prose the worker can follow)
- Has a deterministic **verify_cmd** (exit 0 iff slice succeeded — usually `pytest`, `npm test`, `cargo check`, `ruff && pytest`, or a custom script you write upfront)
- Has a **files-in-scope** list (for --allowed-paths sandbox)

**Good slices:**
- "Add `reverse_string()` in utils.py + 4 pytest cases" (verify: `pytest tests/test_utils.py::TestReverse -q`)
- "Port the existing fibonacci function to use memoization" (verify: `pytest tests/test_fib.py -q && ruff check utils.py`)

**Bad slices (too big/ambiguous):**
- "Add user authentication" (decompose further)
- "Refactor the API layer" (what does "refactored" mean? no verify)
- "Make it faster" (benchmark target missing)

If you cannot write a verify_cmd for a slice, the slice is not ready. Either rework it or escalate that one back to yourself.

**Docker / containerization slices go EARLY, not last.** If the project has a Dockerfile, plan a "build + run + healthcheck" slice immediately after the API slice — not at the end. Docker catches path bugs (missing COPY, wrong WORKDIR, relative path failures) that local dev hides. Deferring Docker to the last slice means you discover these bugs late and have to patch multiple files.

**Gate calibration** — a learned skill. Your acceptance scripts can fail in two directions:
- **Too lenient** (rubiks3d: `grep -q 'scramble'` passed on a blank canvas). Fix: test the VISIBLE OUTPUT, not the file contents. Use Playwright pixel checks for UI, httpx roundtrips for APIs.
- **Too picky** (taskboard: `grep -q "fetch\|const"` failed on valid JS that used `let`). Fix: check SIZE + broad keyword alternation, not exact substrings. If the file is >200 bytes and contains any JS keyword, it's probably fine for a sanity gate.
- **Wrong invariant** (kata-01 slice 1: gate tested `import calc` and the tests, but didn't run `uv run calc add 2 3` — so a missing `[build-system]` block that broke CLI installation slipped through). Fix: when the slice produces a USER INTERFACE (CLI script, HTTP endpoint, MCP tool, library API), the gate must EXERCISE that interface end-to-end, not just verify internals. Module imports prove syntax; CLI invocations prove installation; HTTP requests prove routing; MCP tool calls prove protocol.
- **Meta-rule**: after writing a gate, ask "would this pass on a BROKEN artifact?" and "would this reject a CORRECT artifact?" and "if a user actually used this thing, would they hit a path my gate didn't test?" If any answer is yes, tighten or loosen.

If slicing itself is hard, spawn an Explore subagent to map the codebase first.

### Phase 2 — For each slice, write the acceptance script first, THEN dispatch

For slice N of M:

1. **Write the acceptance script BEFORE goose sees the slice.** This is non-negotiable. Create `<workspace>/.claude-gates/verify_slice_N.sh` (or similar, outside the slice's `--allowed-paths`) with strict assertions. The script:
   - Exits 0 iff the slice actually works the way a user would experience it.
   - Must not include any `|| true`, silent catches, or leniency.
   - Should reuse a preset from `~/.claude/bin/presets/` where applicable (see README there).
   - Example compositions:
     - Python backend slice: `presets/python-strict.sh tests/test_foo.py backend/foo.py`
     - Frontend slice: `presets/frontend-visual.sh http://127.0.0.1:PORT/` (caller starts the server in the script)
     - MCP server slice: `presets/mcp-stdio.sh my_module required_tool1,required_tool2`

2. **Build the scoped brief** (Tier 3 rules). Query omni-mem and/or read local CLAUDE.md for context relevant to this slice's files. Keep brief ≤1500 tokens. Include:
   - Relevant project conventions not in global .goosehints
   - Known gotchas for the files touched (from omni-mem `search`)
   - The slice's acceptance criteria in plain English — described as "what will pass the acceptance script," not as test code
   - Any artifacts from previous slices this one consumes

3. **Invoke the dispatcher** via Bash. Prefer `--acceptance-script` or `--verify-preset` over raw `--verify-cmd`:
   ```bash
   python3 ~/.claude/bin/goose_dispatch.py \
     --slice-id "<track_id>-slice-N" \
     --workspace "<abs path to project or subdir>" \
     --spec "<spec text>" \
     --brief "<scoped brief>" \
     --acceptance-script "<workspace>/.claude-gates/verify_slice_N.sh" \
     --files "<comma-sep file paths>" \
     --allowed-paths "<comma-sep write-allowed prefixes, MUST NOT include the acceptance script>" \
     --max-retries 3 \
     --max-turns 25
   ```
   The dispatcher auto-runs preflight, adds the acceptance script to protected-paths (goose cannot modify it), and scans newly-written test files for cheat patterns after verify.
   Capture the JSON result from stdout.

4. **Interpret the result**:
   - `outcome: pass` → mark slice accepted in auto_runtime if tracking; proceed to slice N+1.
   - `outcome: fail` → the worker couldn't satisfy verify after 3 tries. **Take the slice yourself**: read the evidence_log, read what goose tried, write the correct implementation in-session. Then re-run the acceptance script to confirm. Record in memory why the worker failed (size? domain? reasoning demand?).
   - `outcome: escalate` → sandbox violation or attempt to modify the acceptance script. Do NOT retry. Read the evidence_log, report to the user.
   - `outcome: infra_down` (exit 4) → LM Studio / upstream is unreachable. **Do NOT count this against goose.** Pause the track and re-dispatch only after the user confirms upstream is back.
   - `outcome: gate_cheat_suspected` (exit 5) → verify passed, but test files contain `except: pass`, `assert True`, or similar cheats. Read `gate_cheat_flags` in the output. Rewrite the test yourself OR ask the user. Never accept this outcome as "pass" without review.
   - `outcome: invocation error (exit 3)` → setup issue. Stop and fix.

5. **Do not pause to report between slices** unless a failure requires user input. Log briefly (one line per slice) and continue.

### Phase 3 — Cumulative review

After all slices pass (or are handled):
1. Run the project's full verification suite if it exists (`make test`, full `pytest`, `npm test`).
2. Read the cumulative diff and spot-check:
   - No stray debug code / commented-out blocks
   - No TODO/FIXME added in this pass
   - No scope creep (slices stayed in their lanes)
   - No rule violations that the Tier 4 gates should have caught — if yes, log as a gate-candidate rule to add later
3. If auto_runtime track: mark objective complete with evidence.

### Phase 3.5 — User-journey smoke (NON-NEGOTIABLE)

**Before marking the track complete, execute one end-to-end user journey against the shipped artifact.** Green unit tests ≠ working product. This phase is the primary guard against the "declared done but the frontend was blank" failure.

Write the smoke as a short standalone script and store the artifact it produces in `~/.claude/state/goose_dispatch/<track_id>/smoke/`.

Pick the path that matches your artifact:

- **Web / UI**: start the unified server → Playwright `page.goto("/")` → click the primary user-visible control → screenshot at each stage (solved/scrambled/solved-again for rubiks3d-style UIs) → assert visible state changes matched spec. Reuse `~/.claude/bin/presets/frontend-visual.sh` as the render gate.
- **CLI tool**: run the installed entry-point on its documented happy path with realistic args → capture stdout/stderr → assert exit 0 and expected substring(s).
- **Library**: write a tiny consumer script that imports + exercises the public API → run it → assert non-error plus expected return shape.
- **API / service**: start the server → curl the documented endpoints in sequence (reset → mutate → query) → assert responses match the README / spec.

The meta-rule is: **"If you cannot articulate what a user would do with this artifact and demonstrate you ran exactly that, the track is not complete."** A smoke that just asserts "server returned 200" is too weak — you must exercise the advertised behavior.

Save the smoke artifact (screenshot, stdout transcript, API response log) into the track's `smoke/` directory. Reference it in the track summary.

### Phase 4 — Summary + memory write-back

1. Summarize to the user: slices shipped, slices handled-by-supervisor, files changed, smoke artifact path, any rule gaps observed.
2. Memory write-back:

For the whole track, write to omni-mem:
- `save_memory` for durable lessons (new conventions discovered, gotchas hit)
- `journal_write` for session handoff note
- `fact_add` for factual relationships (e.g., "parse_duration.py supports suffixes s/m/h/d")

Do NOT write ephemeral per-slice state to memory. The evidence logs already capture that.

## Escalation policy

When goose returns `fail` or `escalate`:
- **First escalation**: you handle the slice in-session. Cost: your context window, but you're stronger than the worker.
- **Three escalations in one track**: pause and ask the user whether to continue with goose or switch the remaining slices to `/drive`. The local model isn't the right tool for this task.

Never silently loop on escalation. Never increase max-retries past 3. If the worker fails 3 times, the worker is wrong for this slice.

## Output style

Between slices: one-line status like `slice 3/7: pass (1 attempt, 2 files)`. No ceremony.
On completion: compact summary with counts + list of slices the supervisor had to take.
On abort: explain what slice failed, what you tried, and what decision you're asking from the user.

## Common pitfalls

- **Writing a spec too loose**: "implement fibonacci" → worker may do anything. Instead: "fibonacci(n: int) -> int, fibonacci(0)==0, fibonacci(1)==1, raise ValueError on negative".
- **Not passing a verify_cmd**: the whole system collapses to "trust the worker". Refuse to dispatch a slice without a deterministic verify.
- **Allowed-paths too broad**: `--allowed-paths "."` lets goose modify anything in the workspace. Scope it to exactly the files the slice should touch.
- **Re-reading test files when the task is API-from-spec**: include "DO NOT read the test file" in the spec if the slice is supposed to derive the API from the description (some local models will peek and overfit).
- **Forgetting to reset retry budget between unrelated slices**: each slice gets its own budget. Don't share.

## Files and paths this skill uses

- `~/.claude/bin/goose_dispatch.py` — dispatcher
- `~/.goosehints` — Tier 1 always-on rules
- `~/.config/goose/skills/*.md` — Tier 2 lazy skills (auto-matched by dispatcher)
- `~/.claude/state/goose_dispatch/<slice_id>.log` — per-slice evidence log
- `~/.claude/bin/auto_runtime.py` — governance track manager (if tracking)

---

Remember: you plan, goose executes, the verify-script decides. Do not trust either model's self-report.
