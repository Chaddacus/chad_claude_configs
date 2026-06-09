---
name: codex-chapter
description: Single-round literary editorial review of a drafted manuscript chapter by Codex. Codex reads the chapter against the book's bible (voice, theme, reveal rules, magic-cost, continuity ledger) and the chapter's outline beat, then returns severity-ordered editorial findings + a SHIP/REVISE verdict. Use during chapter-by-chapter drafting to catch voice drift, reveal-tipping, continuity breaks, telling-not-showing, and scene-ownership problems before moving to the next chapter. One round (unlike codex-spar's three). For make-or-break chapters, escalate to full codex-spar instead.
policy_doc_kind: skill
classification: canonical
authority_level: procedural
in_verifier_scope: true
---

# /codex-chapter — Single-Round Chapter Editorial Check (Codex)

A literary cousin of `codex-spar`. Where spar runs a 3-round adversarial debate over a *plan*,
this runs **one** editorial pass over a *drafted chapter* of prose. Codex acts as a developmental/
line editor who has read the book's bible, and returns concrete, severity-ordered notes plus a
ship/revise verdict. Output is a critique file + (after Claude's revision) an accepted chapter.

This skill reviews **prose that already exists**, not a plan. Run it after a chapter is drafted,
before accepting it and moving to the next chapter.

## When to use

- Chapter-by-chapter drafting where you want each chapter checked before continuing.
- Catching the things that drift across a long manuscript: character voice, POV discipline,
  reveal-protection (not tipping a planted twist), continuity vs. earlier chapters, scene-craft.
- After the deterministic prose-tell gate has run (this skill is for what a regex gate *can't* judge).

**Not for:** plans/architecture (use `codex-spar`). **Escalate to `codex-spar` (3-round)** for the
make-or-break chapters where the book lives or dies (voice-establishing opener, midpoint, climax) —
those earn the debate; a transition scene does not.

## Usage

```text
/codex-chapter books/<slug>/manuscript/ch07.md
/codex-chapter --bible books/<slug>/bible --outline books/<slug>/outline.md ch07.md
/codex-chapter --model gpt-5.4 books/<slug>/manuscript/ch15.md
```

## Flags

| Flag | Effect |
|------|--------|
| (none) | Review one chapter against the book's bible/outline/continuity ledger |
| `--bible <dir>` | Bible dir (default: the book's `bible/`) — voice-cards, theme, reveal rules, etc. |
| `--outline <path>` | Outline file, for the chapter's intended beat (default: the book's `outline.md`) |
| `--state <path>` | Continuity ledger / "story so far" (default: the book's `draft-state.md`) |
| `--model <model>` | Override Codex model |
| `--workdir <path>` | Artifacts dir (default `<book>/.codex-chapter/<chapter-id>/`) |

## Codex invocation contract — read before invoking (same as codex-spar)

These failure modes have bitten the Codex skills repeatedly. Do not skip them.

1. **Prompt in a file + `< /dev/null`.** `codex exec [PROMPT]` reads stdin and hangs ("Reading
   additional input from stdin..." → exit 144) when given a positional prompt with open stdin.
   Always write the prompt to a file and redirect stdin from `/dev/null`.
2. **Background + poll, never `tail -f`.** Run codex with `run_in_background: true`, then
   `until [ -f "<workdir>/critique.md" ]; do sleep 5; done`. Tailing locks the Bash tool.
3. **The model writes the critique file.** `-o <file>` only captures the final message text; it does
   NOT create the artifact. The prompt instructs codex to write `critique.md` with its own tools. If
   the sandbox refuses, re-run with `--dangerously-bypass-approvals-and-sandbox` scoped to the workdir.
4. **cwd + trust.** Run from the repo root (so chapter/bible paths resolve) OR pass `-C "<workdir>"`;
   always include `--skip-git-repo-check`.
5. **Flags.** Use `--sandbox workspace-write` (not the deprecated `--full-auto`).

## Workflow (one round)

### Round 0 — Setup + raise the floor
1. Identify the chapter file, its chapter id (e.g. `ch07`), the bible dir, the outline, and the
   continuity ledger. Create `<workdir>/`.
2. **Run the deterministic prose-tell gate FIRST** (cheap, no LLM) and fix what it flags, so Codex
   spends its single pass on craft/voice/continuity rather than mechanical tics:
   ```bash
   producer/.venv/bin/python gates/prose_tells.py <chapter-or-manuscript-dir> --rules <merged-rules.yml>
   ```
   (Merge the global + per-book tell rules if the gate doesn't merge them itself.) Capture the gate
   result to `<workdir>/gate.txt` — Codex is told it already ran, so it won't re-flag tics.
3. **Self-read against the bible** (~5 min): does the chapter hit its outline beat, hold each
   speaker's voice card, and respect the reveal checklist? Fix the obvious misses before sending.
   This is the prose analog of spar's "raise the floor" — it halves the findings Codex has to make.

### Round 1 — Codex editorial pass
Write the prompt to a file, then invoke (background + poll):

```bash
cat > "<workdir>/prompt.txt" <<'PROMPT'
You are a developmental + line EDITOR reviewing ONE drafted chapter of a novel. This is fiction
craft, not code. Your cwd is the repo root. Read, in order:
- <chapter file>                          (the prose under review)
- <outline path>                          (find THIS chapter's intended beat / job)
- <bible>/voice-cards.md                  (how each character must sound)
- <bible>/theme.md                        (the controlling idea — carried by events, never stated)
- <bible>/pov-and-reveal-rules.md         (POV discipline + the per-chapter reveal-protection checklist)
- <bible>/magic-cost-ledger.md            (cost discipline / no power-creep)
- <state path>                            (the "story so far" continuity ledger — do not contradict it)
- <workdir>/gate.txt                      (the deterministic prose-tell gate already ran; do NOT re-flag mechanical tics)
- any other <bible>/*.md you need (arcs, action-craft, conversion-chain, names-registry, festival-and-settings)

Judge the chapter on these axes. For each, give concrete, quotable findings (cite the line/passage):
1. JOB — does it accomplish this chapter's outline beat (what must change by the end)? What's missing/padded?
2. VOICE — does each speaker match their voice card? Flag any line that's off-voice or that blurs two characters.
3. POV & REVEAL — single-POV discipline held? Does anything TIP a protected reveal (check the per-chapter
   reveal checklist for this chapter)? Would a first-time reader take the planted clue at innocent face value?
4. SHOW vs TELL — emotion/stakes dramatized, not narrated? On the big beats, is the prose plain (not purple)?
5. SCENE OWNERSHIP — does the chapter carry its one thread's emotional weight, or try to cash every receipt?
6. CONTINUITY — anything contradicting the "story so far" ledger, the names registry, or established canon?
   Clock-state consistent? Injuries/bond-depth/relationship-position consistent?
7. COST DISCIPLINE — any power used without cost? Any power-creep? (per the magic-cost ledger)
8. PROSE CRAFT — pacing, scene-vs-summary balance, dialogue mechanics, sentence-rhythm, chapter exit/hook.
   (Mechanical tics already gated — only raise a tic if it's a pattern the gate missed.)

No praise, no "overall solid." Severity-ordered findings (CRITICAL / HIGH / MEDIUM / LOW), each citing a
specific passage and proposing a concrete fix. CRITICAL/HIGH = blocking; MEDIUM/LOW = polish.

Write your critique to <workdir>/critique.md using your own shell/write tools. End the file with one line:
"CHAPTER VERDICT: <SHIP | REVISE: <one-line summary of blocking notes>>"
PROMPT

codex exec --sandbox workspace-write --skip-git-repo-check -C "$(pwd)" --json \
  -o "<workdir>/codex.json" \
  "$(cat <workdir>/prompt.txt)" \
  < /dev/null   # run_in_background: true

until [ -f "<workdir>/critique.md" ]; do sleep 5; done
```

Read `<workdir>/critique.md` and show the user the verdict + findings.

### Disposition — Claude revises
- **SHIP:** accept the chapter; apply any MEDIUM/LOW polish worth taking; update the continuity ledger.
- **REVISE:** fix every CRITICAL/HIGH finding in the chapter prose. For any finding you DON'T apply,
  write one sentence why (out of voice for the book, would tip a reveal, contradicts a locked decision).
  Then re-run the check ONCE. **2-attempt cap:** if a chapter can't pass in two revisions, the problem is
  upstream (the outline beat or a bible constraint) — stop and surface it, don't grind a third pass.
- After acceptance, **update the continuity ledger** (`draft-state.md`): what this chapter established,
  clock-state, which reveal seeds were planted (and at what cue-level), character/relationship state, and
  any promise the prose now owes. This is what keeps chapter N+5 from contradicting chapter N.

## Constraints

- **One round.** This skill is deliberately single-pass. If a chapter needs adversarial depth, it's a
  make-or-break chapter — use `codex-spar` (3-round) instead.
- **Codex is the editor, Claude is the author.** Don't argue findings into consensus; either fix the prose
  or document why the note doesn't apply to *this* book's voice/constraints.
- **The bible is the source of truth.** Codex critiques against the bible + outline + ledger, not against
  generic "good writing." A note that contradicts a locked bible decision is a low-signal note.
- **Run the deterministic gate first.** Don't spend Codex's pass on tics a regex catches. The gate is free.
- **Update the ledger every accepted chapter.** Skipping it defeats the purpose — continuity drift is the
  main risk of chapter-by-chapter drafting, and the ledger is the only thing tracking it.
- **Voice and emotional landing still want human eyes.** This skill catches craft/continuity/logic; a
  human read-through at act boundaries remains irreplaceable.

## Failure modes

- **codex exits 144 / hangs on stdin** — prompt-as-positional + open stdin. Use prompt-in-a-file + `< /dev/null`.
- **"Not inside a trusted directory"** — add `--skip-git-repo-check`; run from repo root or `-C`.
- **critique.md not written** — `-o` only captures the final message; re-run with `--dangerously-bypass-approvals-and-sandbox` scoped to the workdir, or write the file from `codex.json`'s final message.
- **Codex re-flags mechanical tics** — it wasn't told the gate ran, or `gate.txt` wasn't provided. Include it.
- **Codex returns vague "tighten the prose" notes** — low-signal; require it to quote the passage and propose the concrete fix (the prompt already demands this; re-run if it didn't comply).
- **Every chapter comes back REVISE on the same axis** — that's an upstream bible/outline problem, not a per-chapter one. Fix the constraint, not 32 chapters.

## Notes
- Pairs with `codex-spar` (reserve the 3-round version for ch1 / midpoint / climax) and the deterministic
  `prose_tells.py` gate (run first, every chapter).
- Workdir (`.codex-chapter/<chapter-id>/`) holds `prompt.txt`, `gate.txt`, `critique.md`, `codex.json` —
  a per-chapter review trail. Add `.codex-chapter/` to `.gitignore` if you want it ephemeral.
- This skill writes a critique and (via Claude) edits the chapter prose; it does not run the book's pipeline
  stations or advance `state.json`.
