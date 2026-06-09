---
name: codex-spar
description: Three-round adversarial plan review between Claude and Codex. Claude plans, Codex critiques as a hostile reviewer, Claude defends and updates, Codex re-attacks, etc. Use before committing to a non-trivial implementation to harden the plan against weaknesses, scope creep, and missed edge cases.
policy_doc_kind: skill
classification: canonical
authority_level: procedural
in_verifier_scope: true
---

# /codex-spar — Adversarial Plan Review (Claude vs. Codex, 3 rounds)

Runs a structured 3-round critique loop between Claude (planner / defender) and Codex (adversarial reviewer) over a written implementation plan. The output is a hardened plan plus the full transcript.

This skill produces a **plan**, not code. Run it *before* `/codex-delegate` or any implementation work.

## When to use

- Non-trivial implementation (>3 files or >500 LOC) where direction is still soft
- Architectural decisions that are expensive to reverse
- Anywhere "I think this approach works" is the current confidence level
- Research/eval pipelines where edge cases matter

**Not for:** trivial fixes, work where the right answer is obvious, or tasks where Codex is doing the implementation (use `/codex-delegate` for that).

## Usage

```text
/codex-spar build a from-scratch numpy MLP for MNIST with hand-derived backprop
/codex-spar refactor the auth middleware to use JWT instead of sessions
/codex-spar --rounds 5 design the eval harness for the reasoning model
/codex-spar --model gpt-5.4 add streaming to the inference server
```

## Flags

| Flag | Effect |
|------|--------|
| (none) | 3 rounds, default Codex model |
| `--rounds <N>` | Override round count (1–5; default 3) |
| `--model <model>` | Override Codex model (e.g. `gpt-5.4-mini`, `gpt-5.4`) |
| `--workdir <path>` | Where to write artifacts (default `.codex-spar/<task-slug>/`) |

## Codex invocation contract — read this before round 1

Two non-obvious failure modes have bitten this skill repeatedly. Avoid both.

**1. Heredoc-as-positional-arg + Bash tool stdin = hang.** `codex exec [PROMPT]` reads stdin and appends it as a `<stdin>` block when a positional prompt is also given. The Bash tool's environment leaves stdin in a state where codex blocks on "Reading additional input from stdin...". **Always pass prompts via a file, and always redirect `< /dev/null`.**

   ```bash
   # GOOD — prompt in a file, stdin closed
   codex exec --sandbox workspace-write --skip-git-repo-check --json \
     -o "<workdir>/codex-r1.json" \
     "$(cat <workdir>/r1-prompt.txt)" \
     < /dev/null
   ```

   ```bash
   # BAD — heredoc + open stdin → hang or "Reading additional input from stdin..." → exit 144
   codex exec --sandbox workspace-write --skip-git-repo-check --json -o "<workdir>/codex-r1.json" "$(cat <<EOF
   ...
   EOF
   )"
   ```

**2. `tail -f` on a background command's output file in the Bash tool will hang the tool.** Don't tail. Run codex in the background, then poll the critique file's existence with a short until-loop. Codex writes the critique via its own tools while running.

   ```bash
   # GOOD — background, then wait on the file the model is told to write
   codex exec --sandbox workspace-write --skip-git-repo-check ... < /dev/null   # run_in_background: true
   until [ -f "<workdir>/critique-r1.md" ]; do sleep 5; done
   ```

   ```bash
   # BAD — tail -f locks the tool
   tail -f /tmp/.../task.output &
   ```

**3. Writing the critique file is the model's job, not codex's.** `-o <FILE>` only captures codex's final message text; it does NOT write the structured artifact. The prompt instructs codex to use its own Write/shell tools to create `critique-r<N>.md`. If `--sandbox workspace-write` isn't enough to write under the workdir, escalate to `--dangerously-bypass-approvals-and-sandbox` for the spar directory only.

**4. Working directory + trust check.** Codex inherits cwd, and outside a trusted git directory it errors out with "Not inside a trusted directory and --skip-git-repo-check was not specified." Two options: (a) run from the repo root so relative paths like `.codex-spar/<slug>/...` resolve, or (b) pass `-C "<workdir>"` to make codex's cwd the spar dir and always include `--skip-git-repo-check`. Option (b) is preferred when the workdir lives outside a git repo (e.g. `~/.codex-spar/<slug>/`). Confirm with `pwd` in the same Bash call if unsure.

**5. Flag deprecation.** `--full-auto` is deprecated; use `--sandbox workspace-write` instead. All examples in this skill use the new flag. If you see `--full-auto` referenced elsewhere, it still works today but emits a warning.

## Raise the floor before round 1

The spar exists to catch what self-audit misses. Every blocker Codex finds is a blocker that *could have been* avoided by raising the floor on what gets sent in. Three practices, mandatory before invoking Codex. They cost ~10 minutes total and typically cut round-1 critical findings in half.

**1. Read before writing (grounding pass).** Before plan-v1 is written, locate every module/function the plan will reference (`rg`, `find`, `Read`). Record canonical path + line number for each function the plan extends/wraps/replaces. Note any divergence from assumed structure (wrong path, missing function, different signature).

Plan-v1 must include grounded references inline. "Will modify `auto_runtime_common.py`" is insufficient. "Will add `_append_phase_event` near `_append_event:162`" is sufficient. Path/function-existence claims that aren't grounded will be flagged by Codex; pre-grounding eliminates the round-1 finding cheaply.

**2. Self-attack with the round-1 adversary prompt.** After plan-v1 is written and before invoking Codex, paste the round-1 adversary prompt (the one defined in 1b) into your own context and read your own plan with hostile eyes. Spend ≥5 minutes. Findings you can defend: fine. Findings that would land: pre-spar fixes — apply to plan-v1 before sending.

Write the self-attack pass to `<workdir>/self-attack-r0.md` (what you looked for, what you found, what you fixed). The spar trail then shows what you caught vs. what Codex caught — useful for improving the floor over time.

Target: round-1 critical findings ≤2. If self-attack catches nothing, you weren't hostile enough — re-read with a different angle (anti-overengineering lens, reuse-first lens, "what's the cheapest module to cut" lens).

**3. Adversarial re-attack on dispositions.** Same pattern applied to plan-v2 and plan-v3 before sending to round 2 and round 3:
- Each ACCEPT: did you actually restructure, or did you relabel/move text? An accept that leaves the original structure intact is usually paper-thin. Codex round 2 has explicit "disposition audit" language for catching this.
- Each REJECT: would a hostile reader find your rejection sentence sufficient? One-line rejections of multi-paragraph critiques are usually weak.
- Each DEFER: genuine (out of scope, blocked) or convenience (don't want to deal)?

Document the re-attack in plan-v2/v3 under "Self re-attack on dispositions."

**Anti-novelty check (runs alongside all three).** For each new module, abstraction, or capability in the plan, ask: is this load-bearing for the *core hypothesis*, or am I including it because it sounds impressive / matches a paper / fills a "field gap"? Cargo-cult inclusions are the most common form of overengineering in plan-v1. If you can't tie it to the core hypothesis in one sentence, cut it pre-spar.

---

## Workflow

All artifacts live under `<workdir>/` (created if absent). One file per round per side.

### Round 0 — Setup

1. Slugify the task description for the workdir name (e.g. "build-numpy-mlp-mnist").
2. Create `<workdir>/` and write `task.md` containing the original task description and any context Claude has from the conversation.
3. If a source proposal/document exists (e.g. user referenced an external file), copy it into `<workdir>/proposal.md` so codex always reads from a workdir-local path.
4. **Run the grounding pass** (Raise the floor §1). Capture findings inline in `task.md` under a "Grounded references" section if discovery is non-trivial.
5. Confirm to user: "Sparring on '<task>'. Workdir: `<workdir>/`. <N> rounds."

### Round 1

**1a. Claude writes the plan** — `<workdir>/plan-v1.md`.

Format (mandatory sections):
- **Goal** — one sentence, the user's actual objective.
- **Decomposition** — list of sub-problems, ordered.
- **Approach per sub-problem** — chosen technique + brief justification.
- **Reuse-first map** — for every proposed new module/file, name the existing function (with `path:line`) it extends/wraps/replaces, or explicitly prove no existing primitive fits in one sentence. Floor practice §1 makes this concrete instead of asserted.
- **Files touched** — concrete paths, expected LOC per file.
- **Verification plan** — how we'll know it works (tests, metrics, manual checks).
- **Known risks / open questions** — what Claude is least sure about.
- **Rejected alternatives** — what Claude considered and dismissed, with reasons. Include any novelty/impressiveness inclusions you cut during the anti-novelty check.

No hedging. State decisions, not options.

**Before sending to 1b: run the self-attack pass** (Raise the floor §2). Write `<workdir>/self-attack-r0.md` documenting what you looked for, what you found, and what you fixed in plan-v1. Apply fixes before invoking Codex.

> **Reviewing existing artifacts (not greenfield planning).** When the task is "critique this proposal/doc/PR", plan-v1.md is Claude's *defense / promotion plan* for the artifact: same mandatory sections, but framed as "what must be true for me to ship this." Decomposition lists the sub-areas of the existing work; rejected alternatives lists framings the doc could have taken but didn't. Don't re-summarize the proposal — codex reads it directly.

**1b. Pass to Codex for adversarial critique.**

Write the prompt to a file first, then pass it as a positional arg with stdin closed:

```bash
cat > "<workdir>/r1-prompt.txt" <<'PROMPT'
You are an adversarial reviewer. Read these files in this order:
- <workdir>/task.md
- <workdir>/proposal.md   (if present — the source artifact under review)
- <workdir>/plan-v1.md    (Claude's plan / defense)

Your job is to find every weakness, not to approve. Specifically attack:

1. Unjustified architectural choices — where could a simpler primitive solve this?
2. Missing edge cases the plan doesn't address
3. Scope creep — features/abstractions that exceed the actual goal
4. Verification gaps — claims of "done" that aren't actually testable
5. Unstated assumptions about data, environment, dependencies, or user behavior
6. Better alternatives the planner ignored or dismissed without sufficient reason
7. Order-of-operations issues — does the decomposition actually compose?

No praise. No "overall this is solid." Severity-ordered findings (CRITICAL / HIGH / MEDIUM / LOW). Each finding cites a specific section of the plan and proposes a concrete change or asks a concrete question.

Write your critique to <workdir>/critique-r1.md using your shell or write tools. End the file with a single line:
"ROUND 1 VERDICT: <BLOCKING | NEEDS-REVISION | ACCEPTABLE-WITH-CHANGES | NO-OBJECTIONS>"
PROMPT

codex exec --sandbox workspace-write --skip-git-repo-check --json \
  -C "<workdir>" \
  -o "<workdir>/codex-r1.json" \
  "$(cat <workdir>/r1-prompt.txt)" \
  < /dev/null
```

Run this with `run_in_background: true` if it might exceed ~3 minutes. Then wait on the artifact:

```bash
until [ -f "<workdir>/critique-r1.md" ]; do sleep 5; done
```

If codex finishes without writing the file (e.g. sandbox refused), inspect `<workdir>/codex-r1.json` for the final message text and either re-run with `--dangerously-bypass-approvals-and-sandbox` scoped to the workdir, or write the critique manually from the captured message.

Read `<workdir>/critique-r1.md` and display to user.

**1c. Claude defends and updates** — `<workdir>/plan-v2.md`.

For *each* finding in `critique-r1.md`, Claude must do exactly one of:

- **Accept** — change the plan; show what changed.
- **Reject** — explain in one sentence why the critique is wrong or out of scope. (Use sparingly; defending too much is a smell.)
- **Defer** — acknowledge the issue but explicitly punt to a future round of work, with reason.

Append a **Response to Round 1** section at the bottom of plan-v2.md listing each finding by ID and the disposition (accept/reject/defer + change made).

**Before sending plan-v2 to round 2: run the disposition re-attack** (Raise the floor §3). For each ACCEPT, check that the plan was actually restructured (not relabeled). For each REJECT, check that the rejection sentence would survive a hostile re-read. For each DEFER, check it's genuine and not convenience. Append a **Self re-attack on dispositions** subsection to plan-v2 listing any dispositions you strengthened on the second pass. This is the pre-spar equivalent of Codex round 2's disposition audit — running it yourself first prevents the disposition audit from finding paper-thin accepts.

### Round 2

**2a. Pass updated plan + prior critique to Codex.** Same invocation contract — prompt to file, `< /dev/null`, background + poll.

```bash
cat > "<workdir>/r2-prompt.txt" <<'PROMPT'
You are still an adversarial reviewer. Read:
- <workdir>/plan-v2.md (updated plan)
- <workdir>/critique-r1.md (your prior round)

Two jobs:

(A) Re-attack: find NEW weaknesses introduced by the v2 changes. Plans often regress when patched — look for it.

(B) Audit dispositions: in plan-v2.md's "Response to Round 1" section, did the planner unfairly REJECT any of your prior findings? Identify rejections you stand behind and explain why the planner's defense is inadequate. Do not relitigate accepted findings.

Same severity scale. Write to <workdir>/critique-r2.md using your shell or write tools. End with:
"ROUND 2 VERDICT: <BLOCKING | NEEDS-REVISION | ACCEPTABLE-WITH-CHANGES | NO-OBJECTIONS>"
PROMPT

codex exec --sandbox workspace-write --skip-git-repo-check --json \
  -C "<workdir>" \
  -o "<workdir>/codex-r2.json" \
  "$(cat <workdir>/r2-prompt.txt)" \
  < /dev/null

until [ -f "<workdir>/critique-r2.md" ]; do sleep 5; done
```

**2b. Claude → `<workdir>/plan-v3.md`** with a **Response to Round 2** section. Run the disposition re-attack (Raise the floor §3) before sending to round 3; append **Self re-attack on dispositions** if any dispositions were strengthened on the second pass.

### Round 3

**3a. Pass to Codex one more time.**

```bash
cat > "<workdir>/r3-prompt.txt" <<'PROMPT'
Final round. Read <workdir>/plan-v3.md and the prior critiques (critique-r1.md, critique-r2.md).

Three jobs:

(A) One last pass for any remaining defects you'd block on if this were code review.
(B) State whether the plan is now ready to execute. If not, list the minimum set of blocking changes.
(C) Predict the most likely failure mode at runtime, given what you now know about the plan.

Write to <workdir>/critique-r3.md using your shell or write tools. End with:
"FINAL VERDICT: <READY | NOT-READY: <reasons>>"
PROMPT

codex exec --sandbox workspace-write --skip-git-repo-check --json \
  -C "<workdir>" \
  -o "<workdir>/codex-r3.json" \
  "$(cat <workdir>/r3-prompt.txt)" \
  < /dev/null

until [ -f "<workdir>/critique-r3.md" ]; do sleep 5; done
```

**3b. Claude finalizes** → `<workdir>/plan-final.md`.

If FINAL VERDICT is READY: incorporate any remaining low-severity tweaks and freeze the plan.

If FINAL VERDICT is NOT-READY: Claude must either (a) make the blocking changes and emit plan-final.md, or (b) explicitly disagree with Codex's blocking call and document the disagreement at the top of plan-final.md. Do not silently ignore a NOT-READY verdict.

### Round 4 — Close

Emit `<workdir>/transcript.md` — a single file concatenating all rounds for review:
```
# Sparring Transcript: <task>
## Task
<task.md>
## Plan v1
<plan-v1.md>
## Critique R1
<critique-r1.md>
## Plan v2
<plan-v2.md>
## Critique R2
<critique-r2.md>
## Plan v3
<plan-v3.md>
## Critique R3
<critique-r3.md>
## Final Plan
<plan-final.md>
```

Report to user:
- Final verdict
- Summary of plan-final.md (what we're going to build)
- Path to workdir
- Any blocking disagreements between Claude and Codex
- Suggested next step (typically `/codex-delegate` or direct implementation)

## Constraints

- **Codex is the adversary, Claude is the defender.** Never invert. The asymmetry is the point — if both sides try to reach consensus, you get a worse plan.
- **Defending a critique by rejecting it is allowed but cheap.** Aim for ≥60% accept rate on round 1. If Claude is rejecting most findings, the plan is probably wrong.
- **Plan files are the source of truth between rounds.** Do not summarize verbally between rounds — pass the actual files.
- **Working tree must be clean** before starting (no uncommitted changes other than the workdir itself). Warn if not.
- **Scope creep guard:** if a critique would expand the work past the original task, Claude should DEFER, not ACCEPT. The skill hardens the *current* task; it doesn't grow it.
- **Floor practices are mandatory, not optional.** Skipping the grounding pass, self-attack, or disposition re-attack (Raise the floor §1–§3) is treated the same as skipping a round. The point of the floor is to raise what Codex sees; without it, the spar wastes rounds on findings the producer could have caught.
- **Track the floor over time.** `self-attack-r0.md` accumulates per spar. After every ~5 spars, scan: which finding categories does self-attack consistently miss? Update this skill or `~/.claude/CLAUDE.md` with patterns the floor is failing to catch.

## Failure modes

- **`codex exec` exits 144 immediately or hangs on "Reading additional input from stdin..."** — you used heredoc-as-positional-arg and didn't close stdin. Re-run with the prompt-in-a-file pattern + `< /dev/null` (see "Codex invocation contract" above). This has burned the skill multiple times; do not skip the redirect.
- **`codex exec` exits with "Not inside a trusted directory and --skip-git-repo-check was not specified"** — the workdir is outside a git repo (e.g. under `~/.codex-spar/`). Add `--skip-git-repo-check` and use `-C "<workdir>"` to set cwd explicitly. All examples in this skill already include both flags.
- **`codex exec` warns "`--full-auto` is deprecated; use `--sandbox workspace-write` instead"** — the warning is correct. Switch to `--sandbox workspace-write`. The skill's examples already use the new flag.
- **Codex finishes but the critique file isn't written** — `-o <file>` only captures the final message text, it doesn't create the structured artifact. Either re-run with `--dangerously-bypass-approvals-and-sandbox` (scoped to the workdir) so codex can write directly, or manually copy the final message text from the `codex-r<N>.json` output into `critique-r<N>.md`.
- **Bash tool locks up on `tail -f`** — never tail a background command's output file from the foreground. Use `run_in_background: true` plus an `until [ -f file ]; do sleep 5; done` poll, or just call codex synchronously when the round is short.
- **Codex `exec` times out or returns no output** — retry once with the same invocation. If still failing, abort the round and report the partial state in workdir.
- **Codex produces a critique that's just style/preferences, not substance** — note it, but skill output should call this out as low-signal.
- **Plan converges to "ACCEPTABLE-WITH-CHANGES" in round 1** — still run all 3 rounds. Round 2/3 routinely surface issues that look fine after round 1.
- **Both sides agree too quickly** — surface this as a warning. Adversarial review that produces no friction is often Codex being polite, not the plan being airtight.

## Notes

- This skill writes files but does not modify any source code or run tests. Implementation happens after, in a separate step.
- Workdir is intended to be committed alongside the implementation PR for review-trail purposes. Add `.codex-spar/` to `.gitignore` only if you want sparring sessions to be ephemeral.
- For tasks where Codex should also *implement* after planning, chain: `/codex-spar <task>` → review plan-final.md → `/codex-delegate using <workdir>/plan-final.md, implement step N`.
- Pairs with `/codex-branch` (post-implementation review) and `/codex-security` (security-specific review) — those are about code; this is about plans.
