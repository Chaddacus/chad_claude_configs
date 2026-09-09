# Subagent Context Passing — Parent-Prompt Pattern

## What this is

Documented pattern for parent agents to pass slice-scoped case-facts to spawned
subagents, addressing Claude Certified Architect Foundations Task 1.3
("subagents do not automatically inherit parent context").

## What does NOT work

Originally this work prototyped a `SubagentStart` hook that read parent state
from a JSON file and emitted `additionalContext` for the spawned subagent.

**That approach is impossible at the platform level.** Per Claude Code's hook
documentation: `SubagentStart` has no decision control and no context-injection
output fields. The hook can fire, can log, can produce side effects — but its
stdout JSON does not reach the subagent. The hook entry and supporting script
were both removed when this was discovered (rollback commit on top of S7).

This is the exact failure mode that Codex's Round 2 critique (HIGH 2) flagged
when reviewing the cert-alignment plan: "S0.6 only proves that `SubagentStart`
fires, not that it supports the design S7 wants to document." The plan added
a stricter preflight test in Round 3 but the test wasn't actually run before
shipping S7. Behavioral verification later revealed the gap.

## What DOES work

The pattern recommended by Codex's Round 1 MEDIUM 2 finding (and aligned with
the cert's Task 1.3 skills statement "Including complete findings from prior
agents directly in the subagent's prompt"):

**Parent agents must include a CASE_FACTS block in every Task tool prompt.**

The Task tool's `prompt` argument is the only channel that lands in the
subagent's initial context. There is no SDK-level "inherit parent context"
mechanism in Claude Code today.

### Recommended Task prompt template

```text
## Slice contract
- slice_id: <id>
- goal: <one-sentence objective>
- owned_files: <paths>
- acceptance_check: <command whose exit-0 means done>

## CASE_FACTS
<bullet list of facts the subagent needs that are NOT in the task prompt
itself: prior agent findings, file contents that matter, decisions made
upstream, escalation context>

## Your task
<the actual instruction>
```

The CASE_FACTS section is the explicit context-passing channel. Without it,
subagents start blind every time.

### Enforcement option

A future `SubagentStop` hook addition could check the subagent's transcript
for presence of a CASE_FACTS block in the parent's Task prompt, and emit
stderr if absent. That's enforcement-by-side-effect rather than enforcement-by-
injection — within `SubagentStop`'s actual capabilities. Not implemented here.

## When this becomes worth revisiting

If a future Claude Code release adds context-injection capability to
`SubagentStart` (or adds a new hook event with that capability), this doc
should be updated and the hook approach re-prototyped.

## Why this doc exists if no code shipped

So the next person who tries to solve cert Task 1.3 doesn't waste a day
re-discovering that `SubagentStart` can't do what its name implies. The
verification step was deferred when this plan shipped, and the rollback
happened only after behavioral testing surfaced the gap.
