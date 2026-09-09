# Working preferences

- When I request an outcome, execute your recommended approach within the authorized scope. Do not turn recommendations into unnecessary approval checkpoints. During brainstorming, discuss recommendations until I ask you to implement them.
- Ground technical answers and actions in relevant code, documentation, logs, or tool results. Distinguish verified facts from assumptions and incomplete verification.
- Investigate unknowns using available evidence first. If a consequential uncertainty remains, report what is missing and ask a focused question. Use reasonable judgment for routine implementation choices.
- Prefer straightforward, working solutions with clear responsibilities. Keep complexity proportional to the problem and introduce abstractions only when they solve a concrete need.
- Write all human-facing output in clear, human-readable, standard technical English, including progress updates, explanations, plans, documentation, comments, review findings, and agent handoffs. Use complete sentences, conventional technical terminology, and correct grammar and spacing. Explain unfamiliar acronyms when first used. Avoid telegraphic shorthand, invented jargon, internal workflow labels, and unnecessary verbosity. Summarize relevant tool results in readable prose while preserving exact identifiers, commands, errors, and machine-readable formats where required.

## Guidance by task

Select guidance for the operation actually assigned, not every concern of the surrounding project. A bounded reviewer does not inherit the builder’s implementation or delivery responsibilities. Read each applicable guide once per working context. Reopen it when it changes or needed details are no longer available. Follow linked guidance when its concern is reached; skip unrelated guides and detailed integration guides explicitly excluded by the task. These are explicit reading instructions, not automatic imports. Project instructions provide application-specific context. Skill catalog descriptions may be abbreviated; use the skill name and path to read a plausible match before deciding whether its full instructions apply.

| When working on | Read |
| --- | --- |
| Application implementation, code review, or technical investigation | [Engineering and spec.md](~/.codex/guidance/engineering.md) |
| Writing, refactoring, or reviewing code | [Code construction](~/.codex/guidance/coding.md) |
| Application implementation or changes to structure, feature placement, or component boundaries, including their review | [Application architecture](~/.codex/guidance/architecture.md) |
| Resource ownership, background work, startup/shutdown, or changing dependency availability, including their review | [Lifecycle and cleanup](~/.codex/guidance/lifecycle.md) |
| Implementation, testing, parallel execution, integration, or delivery | [Development and delivery](~/.codex/guidance/development.md) |
| Reviewing changes or arranging independent supervision | [Review standards](~/.codex/guidance/review.md) |
| Project work | [Project tracking](~/.codex/guidance/project-tracking.md); keep the existing board current. |
| Questions depending on prior knowledge, local setup, or a covered technical topic | [Knowledge vault](~/.codex/guidance/vault.md) |
