---
name: policy-files
description: Reminders that auto-load when editing global Claude policy files (CLAUDE.md, rules, standards).
paths: ["**/.claude/CLAUDE.md", "**/.claude/rules/**", "**/.claude/standards/**"]
---

# Policy File Conventions

These reminders auto-load when editing global policy files. They restate (not extend) policy already
defined in `~/.claude/CLAUDE.md` and enforced by `~/.claude/bin/policy_edit_gate.py`.

## Rules

- **Policy edits are gated.** `policy_edit_gate.py` (PreToolUse on Edit|Write) watches CLAUDE.md,
  route_manifest.json, control_plane.json, agents/*.md, rules/*.md, and the procedural standards
  runbooks. CLAUDE.md/manifest/control-plane run the sync high-stakes lane.
- **Stubs must keep pointing at their destinations.** CLAUDE.md stubs name canonical destination docs
  (e.g. `standards/STOP_GATE_L2.md`, `standards/AUTO_RUNTIME.md`); do not break a stub's pointer
  without moving the content with it.
- **Constitutional vs procedural.** Obligations (WHAT/MUST) belong in CLAUDE.md; procedure (HOW)
  belongs in the standards runbooks. New policy-bearing files declare full frontmatter
  (`policy_doc_kind`, `classification`, `canonical_owner`, `authority_level`, `in_verifier_scope`)
  and justify non-duplication, per `standards/POLICY_OWNERSHIP.md`.
