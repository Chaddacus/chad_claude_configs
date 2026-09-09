---
name: vault-knowledge
description: Find, add, or update reusable knowledge in Chad's private vault, and maintain its llms.txt navigation file. Use when preserving a newly investigated answer or working on vault entries; not for secret storage or project progress tracking.
---

# Vault knowledge

Use the existing checkout identified by `VAULT_ROOT`, the current vault working
directory, or the user's global agent instructions. On Chad's Mac it is
`~/chad_personal/vault`. If unavailable, report that limitation;
do not create a competing vault.

Read the checkout's `AGENTS.md`, `llms.txt`, and the two-computer workflow in
`README.md`. The tracked copy of this skill lives at
`skills/vault-knowledge/SKILL.md`; refresh installed copies when it changes.
For entries, `schema/conventions.md` is the canonical standard and
`schema/entry-template.md` is the template.

Search the relevant domain's catalog and note bodies before adding anything.
Investigate missing answers with evidence. Save concise verified findings or
explicitly unresolved questions, with sources, dates, environment, and limits.
An accepted decision is not proof of implemented behavior.

Use `tools/vault-share edit -- <agent-or-editor>` for a session holding the local
write lock. Keep edits within that session. Save and publish authorized changes
with `tools/vault-share save -m <message> -- <exact-owned-paths>` from within it.
The README explains initialization and recovery. Never collect unrelated edits
into a commit, force-push shared history, or silently discard a conflict. Review
the meaning of merged knowledge even when Git reports a clean text merge.

Author notes in `wiki/`, link them from the domain index, and use the save
workflow's catalog regeneration and lint checks. Read back the saved note and
verify discovery. Report its verification status and whether publication
succeeded or the commit remains local. Offline saves are not shared saves.

Keep `llms.txt` a short navigation file following the conventions. Ordinary new
entries belong in their domain index. Do not enable mirror refresh ownership,
machine generators, or scheduled jobs as a side effect of writing a note.
