# CLAUDE.md Rules Audit — 2026-04-15

Source: `/Users/chadsimon/.claude/CLAUDE.md` (~242 lines, ~3.2k words, ~15k tokens in full form).
Target consumer: goose worker running Qwen3-Coder-30B, receiving bounded slices from a Claude supervisor.

## Tier 1 — Non-negotiables (.goosehints)

Universal safety and tool-hygiene imperatives. Short. No reasoning required.

- Never commit, log, or echo secrets, credentials, tokens, or private data.
- Never force-push, `git reset --hard`, `git checkout --`, or `git clean -f` unless the slice brief explicitly requests it.
- Never push to `main` or `master`.
- Never amend commits unless explicitly asked.
- Never skip git hooks (`--no-verify`, `--no-gpg-sign`) unless explicitly asked.
- New branches use the `codex/` prefix.
- Use `rg` for search, never `grep` or `find` for content.
- Use the project's edit tool, not `sed`/`awk`, for modifying files.
- Read a file before editing it.
- Prefer editing existing files over creating new ones; never create docs unless asked.
- Do not run destructive or off-machine commands without explicit approval.
- Do not revert or overwrite unrelated user changes in the worktree.
- Run project typecheck/tests/lint on changed code before declaring a slice done.
- State verification outcomes as facts ("ran X, got Y, passed/failed"); no hedging ("should work", "probably").
- If tests fail after your change, fix them before returning the slice.
- If an approach fails 3 times, switch approach; do not loop forever.
- Be concise. No filler, no progress narration, no option lists unless a real decision is requested.

## Tier 2 — Lazy-loaded skills

### skill: git-discipline.md
Triggers: any slice touching git state, branches, commits, or PRs.
- Use non-interactive git commands; never pass `-i` to `rebase`/`add`.
- Stage specific files by name; avoid `git add -A`/`git add .` (risk of sweeping in `.env`, build artifacts, credentials).
- Commit messages: concise, imperative, 1–2 sentences focused on the "why" not the "what".
- Do not include `Co-Authored-By` trailers unless the slice brief specifies a co-author.
- Before commit: run `git status`, `git diff`, and `git log -n 5` to understand repo state and message style.
- If a pre-commit hook fails, the commit did NOT happen — fix the issue, re-stage, make a NEW commit. Never `--amend` to paper over a hook failure.
- Respect dirty worktrees: do not `stash` or discard unrelated changes.
- Never rewrite published history on shared branches.

### skill: test-discipline.md
Triggers: any slice that touches `*_test.*`, `tests/`, `spec/`, or production code that has tests.
- Scope verification to the slice: run only the tests covering the changed code until the final slice, then run the full suite.
- Distinguish pre-existing failures from failures you introduced; only fix the latter (flag the former in the slice report).
- Do not delete, skip, or xfail a test to make the suite green unless the slice brief authorizes it.
- Do not replace real integrations with mocks to bypass failures; if the brief requires a real DB/API, keep it real.
- If a test is flaky, re-run once; if still flaky, flag it rather than retry-loop.
- Record the exact command run and its pass/fail status as slice evidence.

### skill: security-basics.md
Triggers: code handling auth, secrets, user input, serialization, crypto, subprocess, network, or file I/O on user-controlled paths.
- Never hardcode secrets, API keys, or tokens; read from env or the project's secret store.
- Validate and sanitize external input before use in SQL, shell, filesystem paths, or template expansion.
- Use parameterized queries; never string-concatenate SQL.
- Never pass user input to `eval`, `exec`, `subprocess(shell=True)`, `os.system`, or equivalents without whitelist validation.
- Prefer the project's existing crypto/auth primitives over rolling new ones.
- Do not log secrets, PII, or full request/response bodies containing credentials.
- Do not weaken CORS, CSRF, TLS, or auth checks to make a test pass.

### skill: python-conventions.md
Triggers: slice touches `*.py`, `pyproject.toml`, `requirements*.txt`, `setup.py/cfg`, or `tox.ini`.
- Match the project's existing style (indent, quote style, import order) — do not reformat unrelated code.
- Use type hints on new public functions when the project already uses them.
- Prefer `pathlib.Path` over `os.path` in new code.
- Use explicit exceptions, not bare `except:`.
- Use f-strings for formatting in new code (unless project uses `%` or `.format` consistently).
- Run `ruff`/`black`/`mypy`/`pytest` according to the project's configured tooling; do not invent a new linter.
- Do not add a new dependency unless the slice brief authorizes it; prefer stdlib first.

### skill: scope-control.md
Triggers: any slice at Tier 2 or above, especially when the code path feels like it "wants" to grow.
- Hard cap: if a slice is drifting past 500 LOC or 3 files, stop and report scope growth to the supervisor.
- Do not introduce a new service, persistence layer, schema family, or orchestration engine inside a slice.
- Reuse existing project primitives before writing new ones; if you cannot prove reuse fails in one sentence, reuse.
- Do not refactor code outside the slice's stated surface.
- Do not add config knobs, feature flags, or abstractions that are not required by the slice brief.
- If you need something outside scope, write a placeholder against the documented shape and flag it; do not expand.

## Tier 3 — Per-slice Claude-curated briefs

Supervisor (Claude) fetches and injects these at dispatch time. Not pre-baked.

- Project-specific file locations, module graph, canonical primitives ("use `core/http.py` not new client"). Trigger: every slice, ≤400 tokens from an omni-mem project pack.
- Current repo conventions beyond Python (language-specific style, framework idioms) when slice touches non-Python code.
- Known gotchas or prior bugs in the files this slice will touch (omni-mem `search` scoped to the file path).
- The slice's acceptance criteria (what "done" looks like for THIS slice), derived from the track's plan.
- The verification command(s) to run for this slice (exact `pytest -k ...` or `npm test path/...` invocation).
- Whether the slice is allowed to add dependencies, touch migrations, or modify public APIs.
- Branch name, base branch, PR target (if the slice produces a commit).
- Any prior slice output artifacts that this slice consumes (e.g. generated schema from slice N−1).
- User-specific preferences from omni-mem `save_preference` (e.g. "this user wants dataclasses, not attrs").
- The slice's budget ceiling (LOC, files, time) — from the auto-runtime track.

## Tier 4 — Gate candidates (scripts, not prompts)

Enforce via tooling; remove from prompt surface entirely.

- "No hedging language in reports" → post-run lint on slice output: `rg -n '(should work|probably|seems correct|I believe)' slice_report.md` fails CI.
- "No print statements in production code" → `ruff` rule `T201` (flake8-print).
- "No bare `except:`" → `ruff` rule `E722`.
- "Type hints on public functions" → `ruff` rules `ANN*` or `mypy --strict` on package boundary.
- "No hardcoded secrets" → pre-commit `detect-secrets` or `gitleaks` hook.
- "No `shell=True` with user input" → `bandit` rule `B602`/`B605`.
- "Commits don't touch `main` directly" → pre-push hook rejecting pushes to `main`.
- "Branch name starts with `codex/`" → pre-push hook, regex `^codex/`.
- "No `--no-verify` / `--amend` in git invocations" → shell wrapper or pre-commit check on agent-issued commands.
- "No `rm -rf`, `git reset --hard`, `git clean -f` from agent" → command allowlist in goose's shell tool.
- "Slice under 500 LOC / 3 files" → post-edit diff-stat gate in the supervisor; reject dispatch return if exceeded.
- "Typecheck/tests/lint must pass" → slice acceptance gate runs the project's configured tooling and refuses `accept` on nonzero exit.
- "Tests don't use mocks where brief says real DB" → custom `grep`/AST script in slice verify.
- "No secrets written to memory store" → omni-mem write-hook scanning for secret patterns.
- "Readiness of infra (manifest, control plane, omni-mem)" → `auto_runtime.py readiness` precheck, already a script.

## DELETE / refactor

These do NOT apply to a goose worker and should not be injected.

- All `auto_runtime.py`, `/drive`, `/build`, `/govern` routing, track lifecycle management — supervisor's job, not goose's.
- Route tiers R1/R2/R3/R4/R5 classifications and dispatch budgets (R2=12, R3=24, R4=40) — orchestration layer, not worker layer.
- `UserPromptSubmit`, `Stop`, `PreCompact` hooks and auto-compact behavior — Claude Code harness only, goose has different lifecycle.
- `planning-gate` skill, Ralph postflight, `finalize_gate.py`, enterprise scorecard, solution ladder (L1/L2/L3), `existing_primitives_considered`, `reuse_first_decision` fields — governance schema belongs in supervisor plan, not worker prompt.
- `classify_prompt.py`, `route_hint`, `governance_recommended` — supervisor-only classification.
- "Default to action over asking", "perpetual motion", "do not stop between slices", anti-stop patterns 1–6 — these are supervisor-loop behaviors. Goose always runs one slice and returns; it has no loop to not-stop.
- "Explore subagent", "Agent tool for parallel subagents", "bounded swarm", `TeamCreate`, `TaskCreate` — supervisor-only orchestration.
- "Send completion notification via `notify_done.sh`" — supervisor concern.
- "Self-Audit / Expert Review" ceremonial sections, "support confidence", "strong/weak/blocked closure" taxonomy — supervisor-level quality framing; translate to slice acceptance evidence only.
- omni-mem write operations (`save_memory`, `save_preference`, `journal_write`, `fact_add`) — supervisor curates memory; worker is stateless.
- `what-would-chad-do` reflection skill — supervisor closure step.
- Policy-doc frontmatter (`policy_doc_kind`, `classification`, `canonical_owner`, `authority_level`, `lexical_guard_profile`) — metadata, not rules.
- Reference index (pointers to runbooks, manifests, POLICY_OWNERSHIP.md) — not operable by worker.
- Ownership boundary narrative (Claude owns `~/.claude`, Codex owns `~/.Codex`) — not relevant to worker.
- Legacy reference artifacts warning (`sync-sources/`, `rules/codex-import/`) — not relevant.
- Memory two-tier model explanation, claude-mem/omni-mem distinction — supervisor concern.
- "Keep global policy concise" — meta-rule about this doc, not about code.
- Duplication between "Execution loop" and "Completion" sections on hedging, evidence, and when-to-stop — consolidate to a single imperative in Tier 1 / Tier 2 test-discipline.
- "Stop hook is a memory checkpoint, not an exit signal" — Claude Code harness semantic; goose has no such hook.

## Summary counts

- Tier 1: 17 rules, ~380 tokens (fits the ≤500 budget).
- Tier 2: 5 skills (`git-discipline`, `test-discipline`, `security-basics`, `python-conventions`, `scope-control`), 37 rules total.
- Tier 3: 10 per-slice brief categories flagged for supervisor curation.
- Tier 4: 15 gate candidates with concrete tool mappings.
- Delete: 18 rule clusters (governance/orchestration/harness-specific) excluded from worker surface.

Net effect: ~15k token CLAUDE.md collapses to ~380 tokens always-on + ≤2k tokens of lazily-loaded skill(s) per slice + ≤1.5k per-slice brief. Qwen3-Coder-30B sees ~2–4k tokens of rules context instead of 15k, preserving attention for the actual coding task.

---

## Regressions prevented (grows over time)

Each entry names a real failure caught in a prior run and the guard that now prevents it. Add a line every time a new failure mode is caught and wired into automation.

| Observed failure | Where observed | Guard now in place |
|---|---|---|
| Goose wrote `except: pass` / `assert True` in tests to cheat verify | rubiks3d slices 6, 8, 11 (visual, animation, MCP) | Post-dispatch cheat scanner in `goose_dispatch.py` (`scan_for_cheats`) returns `outcome: gate_cheat_suspected`, exit 5 |
| Frontend verify gate `grep -q 'scramble'` passed on blank canvas | rubiks3d slice 6 | `~/.claude/bin/presets/frontend-visual.sh` does pixel-distribution check (white_frac<0.05, unique_colors>30) |
| LM Studio outage conflated with logic failure, burned 3 retries | rubiks3d slice 3 | Auto-preflight + mid-retry upstream ping → `outcome: infra_down`, exit 4 |
| Goose hand-coded 18 cube-rotation permutations with subtle sticker-cycle bug | rubiks3d slice 2 | Library-first heuristic appended to `WORKER_TOOL_GUIDANCE` + one line in `.goosehints` |
| `uv sync` build artifacts (.venv) escalated sandbox | rubiks3d slice 1 | Build dirs (.venv, node_modules, .pytest_cache, etc.) excluded from workspace snapshot |
| Qwen3-Coder emits `write` tool with empty arguments on content > 40 lines | rubiks3d slice 2 | `WORKER_TOOL_GUIDANCE` mandates shell heredoc for file writes |
| MCP tools registered with `tool_` prefix pollution | rubiks3d slice 5 | `mcp-stdio` preset validates required tool names; skill reminds to use `@mcp.tool(name="...")` |
| Supervisor declared "done" without running the artifact as a user would | rubiks3d meta-failure | `SKILL.md` Phase 3.5 — mandatory user-journey smoke with artifact captured to `~/.claude/state/goose_dispatch/<track>/smoke/` |
