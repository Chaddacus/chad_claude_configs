# Controlled Promotion & Release Governance (SPEC.md Standard 6)

## Branch model

`main` ← `dev` ← `feature/<feature>` ← subtask/worktree branches. All roads to production go through `dev`. Subtask branches merge into their parent feature (never directly into `dev`); the parent feature is the consolidation point.

## Automation authority

You MAY autonomously: create feature/subtask branches and worktrees; edit, test, commit, create/review PRs; merge verified subbranches into the parent feature; consolidate and verify the parent; merge a fully gated parent feature into `dev`; deploy to and fully validate DEV; prepare promotion evidence and production deployment; execute pre-authorized rollback/runbook actions within exact bounds.

You MUST NOT autonomously: promote `dev` to `main`; deploy new code/config from `main` to production; perform unapproved destructive/irreversible actions.

## Human gates

- **Gate #1:** authorizes an exact grounded revision `dev` → `main`.
- **Gate #2:** authorizes an exact release artifact `main` → production.

Approval binds to the exact revision/artifact and exact transition — any material change invalidates it. Prepare gates with the foundation's packet templates (`templates/release/`); never self-approve, never route around a gate, never weaken project rulesets/environment protections from Claude configuration.

## Release identity

Prefer build-once/promote-same-artifact. Release identity includes source commit/tree, immutable artifact digest, and build provenance/attestation where supported. DEV validates the artifact intended for production where the platform allows.

## Identities

Builder, verifier, and approver are separate trust roles. Automation operates through a dedicated bot/service identity distinct from the human approver (see `templates/release/automation-identity.md`).

## Completion

A feature is not complete at deploy time. LIVE VERIFIED requires live evidence; the release reaches CLOSED only after post-release cleanup (worktrees, branches, processes, scratch artifacts, doc reconciliation, stale-PR closure — `templates/release/post-release-cleanup.md`).
