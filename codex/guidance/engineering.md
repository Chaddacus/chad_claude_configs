# Engineering expectations

## Understand the application

Each application should have one maintained `spec.md` describing its current state. Read it first when working on that application, then inspect the code and other evidence relevant to the task before answering or making changes.

Use `spec.md` as the primary application reference. Historical plans provide background; they are not the authority for current behavior. If the spec and implementation disagree, investigate the discrepancy and reconcile the documentation with verified behavior and the user's intended outcome.

If `spec.md` is missing, establish the application's boundaries and inspect existing documentation and code. Create it as part of authorized implementation work. For a read-only task, report the missing spec without expanding the task into documentation changes.

## Retrieve focused evidence

Use targeted searches and relevant file sections to answer the current question. For guidance excerpts, use `python3.11 ~/.codex/scripts/read-guidance.py --list` to discover headings, then request exact `guide:Section heading` selectors. This prints only the requested sections and their document introductions, with source paths, line ranges, and hashes. Use its output in handoffs instead of loading whole guides to extract a few paragraphs. Retrieve additional sections when their concern is reached. Keep complete logs in files and return the evidence needed for the decision. If output is truncated, retrieve the missing relevant section instead of repeating the same broad dump. Expand inspection when dependencies, failures, or uncertainty require it; do not omit necessary grounding to reduce context.

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
