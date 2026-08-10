# Engineering Foundation — Global Bootstrap

This machine runs the Claude Engineering Foundation. `SPEC.md` in each repository is that repository's canonical truth. Detailed policy lives in `~/.claude/rules/` (installed from the foundation). This file stays concise by design.

## Governing loop

GROUND → TRIAGE → PLAN → ANTICIPATE → EXECUTE → OBSERVE → RE-GROUND → ADAPT → VERIFY → REVIEW → RECONCILE → COMPLETE

The route through the loop is proportional: a trivial change takes the short path (ground, execute, verify, complete); meaningful work takes the full loop.

## Non-negotiables

- Ground before consequential decisions. Handoffs, memory, old plans, and other agents' assertions are claims, not current truth.
- Continue autonomously on defensible, reversible, evidence-supported decisions. Routine permission-seeking is prohibited.
- Escalate only for: material product-direction decisions, destructive/irreversible actions, protected release gates, critical contradictions, genuine consequential ambiguity.
- Never claim a check passed unless it ran and passed.
- Smallest correct change. Every meaningful line must be defensible.
- Use the least expensive adequate model, context, verification, and review. Routing decisions must be defensible.

## Rules

- `rules/engineering-constitution.md` — code quality, commenting, Definition of Done.
- `rules/architecture-contract.md` — spine/modules, contracts, API/MCP-first, SPEC.md, tests.
- `rules/execution-orchestration.md` — grounding, autonomy, triage, parallelism, planning, review, provenance.
- `rules/verification-evidence.md` — FAST/MODULE/FULL tiers, test principles, frontend proof, evidence lifecycle.
- `rules/security-secrets.md` — trust model, secret references (backend map: `secrets-backends.json`), data classes, tool trust, AppSec.
- `rules/release-governance.md` — branch model, automation authority, human gates, release identity, cleanup-to-CLOSED.
- `rules/observability-incidents.md` — OTel correlation, Cases as incident truth, severity vs authority, L1–L4 self-healing ladder, postmortems.
- `rules/ai-engineering.md` — LLM behind deterministic contract, capability contracts, Claude-first gateway seam, mandatory evals (no evals = NOT READY), versioned AI behavior.
- `rules/frontend-standard.md` — UI spine/module split, WCAG 2.2 AA baseline, design-system governance, UI states, browser-proof completion.

---

# This machine

Machine-specific operating layer. The rules above are the engineering standard; this section is the local wiring they run on. Surface index: `~/.claude/standards/REFERENCE_INDEX.md`.

## Ownership and planes

- Claude owns `~/.claude`. Codex owns `~/.Codex` and is canonical there. No policy mirroring runs between them.
- Work plane is `~/chad_work` (CloudWarriors, clients, work VPS fleet). Personal plane is `~/chad_personal` (creative, games, creator stack, personal experiments). Never cross telemetry, secrets, or memory between them.
- The secret backend is resolved per plane by `secrets-backends.json`, never guessed: `op` for the work plane, `rbw` (Bitwarden) elsewhere. Unlocking a vault and creating a vault entry are human acts. Never print a secret value — interpolate inline.

## Memory

- Durable memory lives in omni-mem, split by plane: personal vault on `:8767`, work vault on `:8765`. Route with `~/.omni-mem/bin/omem` (`save`, `journal`, `search`, `status`), which selects the vault by working directory and stamps the agent family.
- `omem` routes by working directory, so run it standalone — never inside a `cd` compound that lands outside the intended plane.
- Save durable conventions, recurring gotchas, and decisions with their reasoning. Do not save ephemeral debugging steps or session-local state.
- Start non-trivial work in a known repository with `omem status --workspace <repo-dirname>`.

## Session wiring

- The `UserPromptSubmit` hook supplies `route_hint` and route-scoped gates. Act on them; they are part of the instruction, not commentary.
- Send a completion notification with `~/.claude/bin/notify_done.sh` before the final response. If that is impossible, say so rather than implying it happened.
- Agents are bound per tree: `chad-work` inside `~/chad_work`, `chad-personal` inside `~/chad_personal`, `chad-agent` for anything sent as Chad. A repository-scoped agent file wins inside its repository.

## Git on this machine

- Branch prefix `codex/`. Never push to `main` unless the project opts in explicitly.
- Never `git reset --hard`, `git checkout --`, or force-push unless asked in words.
- Do not amend commits unless asked. Respect dirty worktrees; never revert unrelated changes.

## Communication

Write to Chad in Simplified Technical English: one idea per sentence, short sentences, active voice, present tense, one term per concept, exact technical names. This covers chat, explanations, and messages sent as Chad. It does not cover code, comments, commit messages, or quoted output. Full ruleset: `~/.claude/standards/OUTPUT_STYLE_STE.md`.

When you need a decision from Chad, state the question, the options, your recommendation, and what each choice means in practice. Technical detail belongs in the artifact, not the ask.
