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

## When the repository does not have this model

The autonomy above is granted against a server-side control, not against good intentions. It assumes the branch model exists, that branch rulesets enforce the protected transitions, and that automation runs as an identity distinct from the human approver — so that a gate is something the server refuses, not something the agent promises. Most repositories do not start there, and some have no `dev` branch at all.

Where that infrastructure is absent, the Standard does not relax and it does not pretend:

- Do not invent a gate that nothing enforces, and do not treat your own checklist as an approval. **An unenforced control described as enforced is worse than an absent one** — it converts an honest risk into a false assurance.
- Use the repository's existing process, and say plainly that the gates are not server-enforced there. "I merged this under the project's normal process, which no ruleset enforces" is a true sentence; "the release gate passed" is not.
- Escalate the *irreversible* transitions to a human even when no server will stop you. Absent enforcement raises the human's role; it does not lower the bar.
- Adding the branch model to a repository is itself a change to how that repository is governed. Propose it; do not perform it silently as a side effect of other work.

Establish which case a repository is in by grounding it, not by inferring it from the presence of a `dev` branch. `templates/release/verify-gates.mjs` answers it against live GitHub, and it needs the **target repository's** `gate-config.json` — a declaration lives with the repository it governs, not with the foundation. Run it from a working copy of the target, or pass the path:

```
node <foundation>/templates/release/verify-gates.mjs <owner/repo> [path/to/gate-config.json]
```

Run from anywhere else, the config path falls back to `gate-config.json` in the current directory, which is absent — so the command reports "declares no gates" for every repository including one whose gates are fully configured. A grounding command that returns the same answer regardless of what it is pointed at is worse than no command.

| exit | meaning |
|---|---|
| 0 | every declared gate is really configured on the server |
| 1 | drift, or the live state could not be read: a declared control is missing, weaker, or unverifiable — and a config that declares no checks at all |
| 2 | the invocation was wrong: no `<owner/repo>`, or a `gate-config.json` that is not readable JSON |
| 3 | no `gate-config.json` exists, so the repository is in the case above |

Only 0 is a pass. "Could not verify" is never "verified" — which is why an unreadable ruleset exits 1 alongside real drift rather than getting a softer code of its own.

## Release identity

Prefer build-once/promote-same-artifact. Release identity includes source commit/tree, immutable artifact digest, and build provenance/attestation where supported. DEV validates the artifact intended for production where the platform allows.

## Identities

Builder, verifier, and approver are separate trust roles. Automation operates through a dedicated bot/service identity distinct from the human approver (see `templates/release/automation-identity.md`).

## Completion

A feature is not complete at deploy time. LIVE VERIFIED requires live evidence; the release reaches CLOSED only after post-release cleanup (worktrees, branches, processes, scratch artifacts, doc reconciliation, stale-PR closure — `templates/release/post-release-cleanup.md`).
