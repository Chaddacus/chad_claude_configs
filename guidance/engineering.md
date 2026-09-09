# Engineering expectations

## Understand the application

Each application should have one maintained `spec.md` describing its current state. Read it first when working on that application, then inspect the code and other evidence relevant to the task before answering or making changes.

Use `spec.md` as the primary application reference. Historical plans provide background; they are not the authority for current behavior. If the spec and implementation disagree, investigate the discrepancy and reconcile the documentation with verified behavior and the user's intended outcome.

If `spec.md` is missing, establish the application's boundaries and inspect existing documentation and code. Create it as part of authorized implementation work. For a read-only task, report the missing spec without expanding the task into documentation changes.

## Retrieve focused evidence

Choose the question a read must answer. Search for relevant symbols, then inspect their implementations and callers. When a small set of related files is already known, read it in one bounded batch. Do not repeatedly search for paths already established or load unrelated material to fill a handoff.

Return useful tool output with the status needed to interpret it. Avoid wrapping plain command output in another serialized transport object. Preserve exit status and, for running commands, the session identifier needed to continue them.

For verbose checks, keep complete output in a file and return the command, tested revision or snapshot, exit status, relevant result counts, and failure details with the log path. Include the failed check, actionable error, and relevant source location; retain exception context when it explains the cause. Retrieve more of the log when needed. Do not hide failures, invent a successful summary, or replace a short useful result with a longer wrapper.

If output is truncated, retrieve the missing relevant section instead of repeating the broad dump. Expand inspection when dependencies, failures, or uncertainty require it; do not omit necessary grounding to reduce context.

For guidance excerpts, use `python3.11 ~/.claude/scripts/read-guidance.py --list` to discover headings, then request exact `guide:Section heading` selectors. This prints requested sections and their document introductions with source paths, line ranges, and hashes. Reuse its output rather than rereading complete guides; retrieve additional sections when their concern is reached.

## Maintain spec.md

Keep one `spec.md` at each application's root. In a repository containing multiple applications, each application has its own spec. Identify and maintain an existing application spec rather than creating competing copies.

Describe:

- The application's purpose and current capabilities.
- Its architecture, major components, and important file locations.
- Main user and data flows, interfaces, and dependencies.
- How to configure, run, observe, debug, and verify the application, including API and MCP access.
- Known limitations and unresolved questions.

Keep the document focused on the current application. Clearly distinguish intended behavior that is not yet implemented from behavior verified in the code. Update affected sections in the same work whenever a change alters this description. Do not rewrite the spec for changes that leave its contents accurate.

Preserve accepted requirements when documenting actual behavior. Record a mismatch as a defect or unresolved decision; do not redefine success to match a broken implementation. Keep `spec.md` as the maintained application entry point and link to detailed schemas or references rather than duplicating them.

## Verify completion

Verify the requested behavior with checks appropriate to the change and complete the application's required checks. Report what was verified and any remaining limitations. Before finishing, ensure affected comments and `spec.md` still match the application.

Keep credentials and sensitive production data out of specs, test fixtures, logs, screenshots, and review artifacts. Use secret references and synthetic or appropriately sanitized test data. Use existing secret-scanning tools where available without treating a clean scan as proof that every possible secret was detected.
