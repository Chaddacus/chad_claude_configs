---
name: deep-research
description: Rigorous, evidence-bound web + documentation research that never launders model-memory or a hallucinated fetch into a "fact." WebSearch to DISCOVER, curl to VERIFY, cite the real URL, tag every claim by verification tier, drop anything you can't ground. Use whenever a decision needs grounding in real external sources (genre/market research, technical/API docs, lifecycle/business facts, science). Produces a sourced research doc, not opinions.
policy_doc_kind: skill
classification: canonical
authority_level: procedural
in_verifier_scope: true
context: fork
---

# deep-research — Evidence-Bound Web & Documentation Research

The job: turn a research question into a **sourced, verifiable** document — not a confident essay
from training memory. This skill exists because the single most damaging research failure is
*writing it from memory and dressing it up with plausible-looking citations.* Every load-bearing
claim must trace to a source that was actually fetched and read.

The core discipline: a search result is a lead, not a fact; a fetched-and-read page is a fact. The
distinction is enforced by tiers and by never trusting a model-synthesized fetch body.

## The non-negotiable loop

1. **DISCOVER with `WebSearch`.** Search to find *what to read* — candidate sources, the names of
   primary docs, who the authorities are. Search results are leads, not facts.
2. **FETCH with `curl` (Bash).** Download the actual page/PDF to disk. `curl -sL "<url>" -o <file>`
   (or `curl -sL "<url>" | head -c 200000`). Then **`Read` the file** to confirm it's real content,
   not a 403/404/JS-shell/paywall.
3. **VERIFY before citing.** A claim is only "verified" if you fetched the source and read the
   sentence that supports it. Quote or closely paraphrase from the fetched text.
4. **TAG every claim by tier** (carry the tag into the output doc):
   - `(curl)` — full page body fetched + the supporting text read.
   - `[snippet]` — only a WebSearch result snippet confirmed it (not the full page). Weaker.
   - `[UNVERIFIED]` — could not confirm against a fetched source. State it as unverified or DROP it.
5. **DROP what you can't ground.** A plausible claim with no fetched source is worth less than
   nothing — it poisons the doc. Cut it or mark `[UNVERIFIED]`. Never round a `[snippet]` up to a
   fact.

## Hard rules

- **NEVER cite from WebFetch's body.** WebFetch returns a model-synthesized summary that can invent
  a clean "200 OK" body for a URL that 404s, paywalls, or never existed. WebFetch is allowed ONLY as
  a fast discovery skim to decide whether a page is worth curling — its output is **never** a
  citation source. The authoritative read is always `curl` → `Read`.
- **Cite real URLs**, copied from what you fetched — not reconstructed from memory.
- **Primary > secondary.** Prefer the official doc / spec / first-party page over a blog summarizing
  it. When you only have a secondary source, say so.
- **Quotes must be verbatim** from the fetched text. If you can't find the exact wording, don't
  present it as a quote — paraphrase and tag the tier.
- **Separate fact from inference.** "The doc says X" (cited) vs "therefore we should Y" (your
  analysis) must be visibly distinct.
- **Recency matters.** Note the fetch date; flag when a source is old enough that it may be stale.

## Output shape

Write a durable doc in the repo's research location (commonly `docs/research/<topic>.md`; reference
packets → a `references/` dir). Structure:

- A short **answer/spine** up top (what the research concludes).
- The **body**, every load-bearing claim carrying its `(curl)` / `[snippet]` / `[UNVERIFIED]` tag.
- A **Sources** section: real URLs, each with its verification tier and a one-line note.
- An explicit **Unverified / weak** list — the claims you could NOT ground, flagged so nobody
  downstream treats them as settled.
- If the research maps to a decision in the current project, a short **"application"** section
  separating the cited facts from the recommendation.

## When to use / not use

- **Use:** any non-trivial question that should be grounded in real external sources — genre/market
  research, technical or API documentation, lifecycle/business facts, science, "is this still true."
- **Don't use:** facts discoverable in the current repo (read the code/docs), or trivial lookups.
  This skill is for *external* grounding done rigorously.

## Self-check before delivering

- [ ] Every load-bearing claim has a tier tag, and every `(curl)` claim maps to a URL I actually fetched.
- [ ] No claim is sourced from WebFetch's body.
- [ ] The Sources list contains only URLs I fetched (or, for `[snippet]`, that WebSearch actually returned).
- [ ] The Unverified/weak list is populated honestly (if it's empty, did I really verify everything?).
- [ ] Fact and inference are visibly separated.
