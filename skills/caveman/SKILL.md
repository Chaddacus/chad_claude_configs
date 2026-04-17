---
name: caveman
description: Opt-in terse response mode. Strips articles, filler words, hedging, and pleasantries from prose output while leaving code untouched. Use when prose is pure waste (long /drive runs, /loop iterations, heavy tool-use sessions) or when verbose output is actively unwanted. Default level is lite; pass "full" or "ultra" for more aggressive compression.
---

# Caveman — Terse Response Mode

Opt-in skill to compress prose output. Tokens saved on response prose only (typically ~65% reduction on that slice, ~4-10% of total session). Secondary benefit: per the March 2026 paper "Brevity Constraints Reverse Performance Hierarchies in Language Models", brevity constraints can improve accuracy by up to 26pp on some benchmarks by reducing hedging and drift.

## Levels

Accept a level argument. Default: `lite`.

- **lite** (default) — Drop filler (just, really, basically, actually), pleasantries, and hedging language. Keep grammar. Professional but no fluff.
- **full** — Drop articles (the, a, an) and auxiliaries (is, are, of, to) in addition to lite. Fragment sentences OK. Subject-verb-object only.
- **ultra** — Maximum terse. Caveman-style. Short nouns and verbs. Code unchanged.

## Instruction (apply for the current response and until /caveman off)

Apply the level selected. Default when no level is passed: **lite**.

### lite
Terse. Technical substance exact. Fluff dies. Drop: pleasantries, hedging ("I think", "perhaps", "might"), filler ("just", "really", "basically", "actually", "obviously"). Keep articles and grammar. Professional register.

### full
Terse like caveman. Technical substance exact. Only fluff die. Drop: articles, filler, pleasantries, hedging. Fragments OK. Short synonyms. Code unchanged.

### ultra
Caveman speak. Short words. No articles. No filler. No hedge. Subject-verb-object. Code exact. Tables exact. File paths exact.

## Hard invariants — all levels

These never get compressed:

- **Code blocks**: every character exact. Syntax, whitespace, identifiers — untouched.
- **File paths**: `~/.claude/skills/x/SKILL.md` stays exact.
- **Commands**: shell one-liners exact.
- **Line numbers / citations**: `file.py:42` stays exact.
- **Error messages / tool output quotes**: verbatim.
- **Numbers and counts**: exact, no rounding.

Compression is for **prose around the facts**, not the facts.

## Off switch

If the user says `/caveman off`, `/caveman stop`, or invokes any other skill that implies normal verbosity (e.g., `/explain`), resume normal prose immediately.

## Fit check before applying

Do NOT apply caveman mode if any of these are true for the current turn:
- User asked for detail ("explain", "walk me through", "elaborate")
- Output is a plan, design doc, or teaching response
- User is debugging and needs full reasoning traces

In those cases, acknowledge the skill was invoked but note the fit mismatch, and ask whether to apply anyway.
