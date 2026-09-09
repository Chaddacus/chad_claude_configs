---
policy_doc_kind: ownership_precedence
classification: canonical
canonical_owner: self
authority_level: constitutional
in_verifier_scope: true
lexical_guard_profile: stale_names,branch_policy_live
---

# Policy Ownership And Precedence

This document is the maintenance guardrail for Codex policy changes.

## Canonical Owners

- Runtime configuration:
  - [config.toml](/Users/chadsimon/.claude/settings.json)
  - Owns model defaults, MCP servers, trust settings, and agent availability.
- Runtime contract:
  - [route_manifest.json](/Users/chadsimon/.claude/state/route_manifest.json)
  - Owns route classes, execution profiles, governed-runtime rules, runtime artifacts, and postflight enforcement.
- Universal human-readable policy:
  - [CLAUDE.md](/Users/chadsimon/.claude/CLAUDE.md)
  - Owns safety, communication, review expectations, route summary, and when non-trivial gates apply.
- Workspace-local behavior:
  - project-level `CLAUDE.md` (per repo: `<repo>/CLAUDE.md` or `<repo>/.claude/`)
  - Owns session ritual, local memory workflow, heartbeat behavior, local notification rules, and other workspace-only overrides.
- Task workflow:
  - skills under `/Users/chadsimon/.agents/skills` and `/Users/chadsimon/.claude/skills`
  - Own task-specific workflows, inputs/outputs, and unique task-specific restrictions only.
- Long-form procedure:
  - standards docs and skill references
  - Own detailed runbooks, examples, audit criteria, and procedural explanations.

## Precedence

1. System, developer, and runtime constraints
2. Runtime manifest/config/scripts for their machine-enforced domains
3. Workspace/project AGENTS for local overrides
4. Global AGENTS for universal human-readable policy
5. Skill workflow instructions
6. Standards/reference docs

Human-readable AGENTS policy must not attempt to override machine-enforced runtime behavior. If AGENTS text conflicts with manifest/config/script behavior, the runtime surface wins.

## Placement Rule

- If a rule is machine-enforced or consumed by tooling, put it in config, manifest, or a script.
- If a rule is universal behavioral policy, put it in global AGENTS.
- If a rule is workspace-local, put it in workspace AGENTS.
- If a rule is task-specific, put it in a skill.
- If a rule is long procedural detail, put it in a standards or reference doc.

## Anti-Duplication Rules

- Do not restate route-manifest detail in AGENTS files.
- Do not restate long planning-gate or Ralph procedures in AGENTS files.
- Do not let skills restate generic git policy, global review policy, or runtime routing policy.
- Prefer a short reference to the canonical source over duplicating the rule text.
- Skills are workflows, not constitutions.

### Non-Canonical File Rule

- `active supporting doc`: allowed only when it points to a real canonical owner path.
- `task artifact`: allowed only when it declares itself ephemeral and explicitly says it is not standing policy.
- Free-form pseudo-owners such as `current run`, `current improvement pass`, `session`, or similar labels are invalid.

## Maintenance Loop

- Every new policy-bearing file must name its canonical owner or explicitly declare itself non-canonical.
- Every new policy-bearing file must justify why it is not a duplicate of an existing canonical surface.
- Every change to `~/.claude` policy files (CLAUDE.md, standards docs, policy-bearing skills, `rules/*.md`) must run `~/.claude/bin/policy_pointer_check.py` to confirm no canonical pointer is left dangling. The legacy Codex-home checker `/Users/chadsimon/code/scripts/check_codex_policy_consistency.py` is Codex-scoped and currently non-functional (its index `/Users/chadsimon/code/docs/policy-index.json` has been stale since 2026-03-11 and names dead Codex-era paths); repairing it is the Codex home's responsibility, not a gate for `~/.claude` edits.
- During periodic pruning, search for duplicate ownership/inventory docs, trim new policy prose back to its canonical layer, remove obsolete task artifacts, and update the inventory when the canonical surface changes.

## Known Canonical References

- Prompt contracts:
  - [PROMPT_CONTRACTS.md](/Users/chadsimon/.claude/skills/memory-adaptation/references/PROMPT_CONTRACTS.md)
- Planning-gate workflow:
  - [planning-gate](/Users/chadsimon/.claude/skills/planning-gate/SKILL.md)
- Ralph postflight:
  - [RALPH_LOOP_RUNBOOK.md](/Users/chadsimon/.claude/standards/RALPH_LOOP_RUNBOOK.md)
- Enterprise maturity rubric:
  - [enterprise-maturity-rubric-generic.md](/Users/chadsimon/.claude/standards/enterprise-maturity-rubric-generic.md)
