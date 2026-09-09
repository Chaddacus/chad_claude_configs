---
policy_doc_kind: invariants
classification: canonical
canonical_owner: self
authority_level: constitutional
in_verifier_scope: true
---

# Chad Runtime Invariants

These invariants define the Claude-runtime process architecture. A behavioral change is an architecture improvement only when it strengthens one or more of these invariants and proves that with evidence.

Modeled on `~/automation_architecture/docs/ARCHITECTURE_INVARIANTS.md` (the AgentOps Operating Contract). The AgentOps mapping column shows the deterministic-Kernel analog for each rule — Chad's runtime is prompt-driven where AgentOps is Kernel-deterministic, so several Chad invariants are **convention-enforced** rather than **gate-enforced**. The Status column records that distinction.

Status legend:
- ✅ **enforced** — automated gate, hook, or built-in priority rejects violations
- ⚠️ **convention** — documented rule, no automated gate (relies on agent compliance)
- ❌ **gap** — not enforced and no documented rule today; on the build queue

| ID | Severity | Owner Plane | Status | Required Evidence | AgentOps Analog | Statement |
| --- | --- | --- | --- | --- | --- | --- |
| `CR-INV-001-CLAUDE-MD-IMMUTABLE` | critical | Self / Constitutional | ✅ enforced | user-message string match before edit | INV-KERNEL-GATES | Edits to `~/.claude/CLAUDE.md` require an explicit user request in the active conversation. Skills, agents, hooks, and automated pipelines must not modify this file. The constitutional policy doc is the source of truth that all other artifacts derive from; rewriting it without user authorship breaks the dependency chain. |
| `CR-INV-002-EXTERNAL-SEND-IDEMPOTENT` | high | External | ⚠️ convention with tooling | `~/.claude/bin/idempotency_keys.py` wired into chad-agent `zoom_client.py` send paths; `~/.claude/state/idempotency.jsonl` claim log entry | INV-SIDE-EFFECT-IDEMPOTENCY | Every chad-agent or hook-initiated external side effect (Zoom DM, Zoom channel message, Slack post, email, GitHub comment) must carry an idempotency key keyed on (op, recipient, content_hash) over a configurable window. Re-running a Stop hook, retrying a failed send, or replaying an autonomous loop must not produce a duplicate external message. Implemented 2026-05-16: `~/.claude/bin/idempotency_keys.py` portable callable, wired into chad-agent `zoom_client.py` (466 unit tests pass). |
| `CR-INV-003-WORKER-NO-SELF-MERGE` | critical | Execution | ⚠️ convention with advisory hook | reviewer agent name != worker agent name on accept event; `~/.claude/bin/self_merge_check.py` Stop-hook emits advisory when transcript shows worker Task dispatches + merge-shaped Bash + zero reviewer Task | INV-FLEET-NO-SELF-MERGE | Worker subagents produce artifacts and reports only after dispatch by a parent supervisor agent (the orchestrating session in supervisor mode — standards/ORCHESTRATION_PLAYBOOK.md). Workers cannot accept, merge, or close their own work. Acceptance authority is the parent supervisor reviewing worker output. AgentOps formalizes this as Captain → Fleet with Captain holding integration authority; Chad's runtime relies on the playbook's prompt-level reviewer barrier plus the advisory Stop hook wired 2026-05-16. |
| `CR-INV-004-DENY-WINS-OVER-ALLOW` | critical | Security | ✅ enforced (built-in) | Claude Code permissions resolution order | INV-SECURITY-HARD-DENY | `permissions.deny` entries in `~/.claude/settings.json` cannot be circumvented by allow rules, ask prompts, scoped allows, or `--dangerously-skip-permissions`. Deny is the highest-priority safety primitive. Current deny set: destructive `rm`/`git push --force` shapes plus 14 credential-read rules (`.env*`, `**/credentials*`, `~/.ssh/id_*`, `~/.aws/credentials`, etc). |  <!-- pointer-check:skip -->
| `CR-INV-005-MEMORY-CITES-EVIDENCE` | high | Memory | ⚠️ convention | omni-mem observation/journal/fact record references at least one file path, commit sha, or trace ref | INV-MEMORY-CITES-TRACE | Every `save_memory`, `journal_write`, and `fact_add` should reference verifiable evidence (file path, commit sha, observation id, trace ref). Failure memory records what failed and why; it does not promote failed assumptions as project truth. Current state: omni-mem observation graph exists but enforcement is convention-only — `save_preference` is exempt because preferences are not factual claims. |
| `CR-INV-006-AUTONOMOUS-NO-FALSE-CLOSURE` | critical | Verification | ✅ enforced | `completion_gate.py` verification-evidence ledger; Stop hook anti-stop-patterns | INV-REPLAYABLE-CLOSURE | Autonomous loops (auto_runtime, ralph_done_loop, /drive, /govern, scheduled tasks) cannot declare an objective complete without verification evidence — test output, typecheck output, or explicit verification commands run in-session. Hedging language ("should work", "probably passes", "seems correct") is banned in completion reports. Stop-hook `AUTO-SAVE` is a memory checkpoint, not an exit signal. |
| `CR-INV-007-PROJECTION-REDACTED` | high | External | ⚠️ convention with tooling | `~/.claude/bin/redact_projection.py` callable on send paths (`--strict` exit 11 on hit); documented manual redaction for narrative surfaces (zoom-no-tables, chadacus.dev professional variant) | INV-PROJECTION-REDACTED | Outbound external messages (Zoom, Slack, email, public blog posts on chadacus.dev) must strip internal agent names, absolute file paths, raw prompts, and Chad-internal jargon. Detailed sovereign content stays local (`~/.claude/reports/`, `digital-twin/logs/`, `.agentops/runs/`); external surfaces get scrubbed projections. Portable `redact_projection.py` shipped 2026-05-16 with conservative DEFAULT_PATTERNS (`/Users/` paths, chad-{twin,agent,fleet}, internal slash commands, CR-INV-### ids, internal repo names, `.agentops/` paths). Send-path callers must invoke `redact()` or pipe through the CLI before transmission. |
| `CR-INV-008-DISPATCH-BUDGET-BOUND` | high | Dispatch | ✅ enforced | `route_manifest.json` rule budgets; `auto_runtime.py` cycle counters | INV-BUDGET-BEFORE-DISPATCH | Dispatch budgets are enforced per route class: R1=6 cycles, R2=12, R3=24, R4=40. Repeated failures trigger route promotion (R2 → R3 → R4) rather than infinite retry. Budgets are reserved before subagent dispatch and reconciled on track close. |
| `CR-INV-009-REPLAN-CITES-EVIDENCE` | medium | Control | ✅ enforced (sentinel + strict hook) | omni-mem `journal_write` of shape `{trigger, candidates_scored, threshold, selected, rejected_reasons, rationale}` under topic `replan-*`; sentinel file `/tmp/claude-replan-pending-<session>.json` written by the supervisor BEFORE re-dispatching when 2-attempt rule fires; `~/.claude/bin/replan_evidence_check.py --strict` Stop-hook exits 2 (blocks Stop) when sentinel exists with no matching `replan-*` journal entry | INV-REPLAN-CITES-EVIDENCE | When a supervising run pivots approach (2-attempt rule fires, blocker requires re-decomposition, scope change forced by new constraint), the pivot must be recorded as a structured decision — not narrative. Captures longitudinal signal on which approaches keep failing in which task shapes. See `~/.claude/standards/REPLAN_DECISION_PROTOCOL.md` for the journal_write shape and sentinel protocol. Sentinel-file enforcement shipped 2026-05-16: the orchestration playbook instructs sentinel write at the 2-attempt rule; Stop hook in strict mode blocks session close until journal entry exists. |
| `CR-INV-010-SCOPE-GATE-500-3` | high | Engineering | ⚠️ convention | supervisor self-audit (ORCHESTRATION_PLAYBOOK.md scope gate) + CLAUDE.md core rule | none direct (AgentOps uses budget envelopes; Chad uses LOC envelope) | A proposed change exceeding 500 LOC or 3 files stops and justifies before implementation. Unjustified scope growth is a defect. This is the anti-overengineering gate in operational form. |
| `CR-INV-011-SIMPLE-IS-BETTER` | medium | Engineering | ⚠️ convention | playbook review posture; debugging surgical-not-broad rule | INV-KERNEL-GATES (architecture half: no DB until JSON proves insufficient) | The right amount of complexity is the minimum needed for the current task. Debugging finds the minimal fix first — if moving one line fixes the race condition, move the line. Don't design a system around a symptom. Three similar lines of code is better than a premature abstraction. AgentOps codifies this as the no-DB rule; Chad's runtime codifies it as review posture. |

## Improvement Gate

A change is a runtime improvement only when:
1. It names the target invariant id(s) before implementation.
2. It produces evidence (test, hook log, memory record, replay, projection, operator action) that the invariant is **stronger** after the change — moved from gap → convention, or convention → enforced.

A change that adds machinery without strengthening a named invariant is overengineering and fails the anti-overengineering gate.

## Status Roll-up

- **Enforced**: 5 invariants (001, 004, 006, 008, 009)
- **Convention with tooling/hook**: 3 invariants (002 idempotency module wired into `chad-agent/chad_agent/zoom_client.py` send_dm + send_message; 003 self-merge advisory Stop hook; 007 portable `redact_projection.py` shipped)
- **Convention-only**: 3 invariants (005, 010, 011)
- **Gap (build queue)**: 0 invariants

2026-05-16 promotions:
- 002: gap → convention-with-tooling (idempotency module + chad-agent zoom_client wiring, 466 unit tests pass)
- 003: convention → convention-with-advisory-hook (`self_merge_check.py` Stop hook)
- 007: convention-partial → convention-with-tooling (`redact_projection.py` portable callable)
- 009: convention → enforced (sentinel-file + `replan_evidence_check.py --strict` Stop hook)

## Cross-Cutting Gates

- Every invariant violation logged via hook or memory must be triagable from `~/.claude/state/` JSONL streams without external storage.
- Every gap closure (gap → convention) requires an updated row in this table with a non-empty Required Evidence column.
- Every convention promotion (convention → enforced) requires a passing automated check that rejects violations.
- This document and `~/automation_architecture/docs/ARCHITECTURE_INVARIANTS.md` should be reviewed for drift quarterly — Chad's runtime invariants are a subset/projection of the AgentOps invariants, not an independent rule system.

## Drift Audit

| Date | Action | Invariant(s) |
| --- | --- | --- |
| 2026-05-16 | Initial draft; harvested from AgentOps INVARIANTS doc + chad-twin rules + CLAUDE.md + settings.json + route_manifest.json + persistent memory. | All |
| 2026-05-16 | Promoted CR-INV-002 from gap to convention-with-tooling — `~/.claude/bin/idempotency_keys.py` portable callable shipped. | 002 |
| 2026-05-16 | Wired chad-agent zoom_client to use idempotency module (branch `codex/idempotency-zoom-sends`, 466 unit tests pass). | 002 |
| 2026-05-16 | Promoted CR-INV-009 from gap to convention-with-advisory-hook — `~/.claude/bin/replan_evidence_check.py` wired into Stop hook list in `settings.json`; profile gating via `hook_profile.py` (added to standard + strict allowlists). | 009 |
| 2026-05-16 | Promoted CR-INV-009 to **enforced** — added sentinel-file pattern (`/tmp/claude-replan-pending-<session>.json`) + `--strict` flag to `replan_evidence_check.py` (exit 2 blocks Stop when sentinel exists with no `replan-*` journal entry); chad-twin agent definition amended to write sentinel before re-dispatching at 2-attempt rule. | 009 |
| 2026-05-16 | Promoted CR-INV-003 to convention-with-advisory-hook — `~/.claude/bin/self_merge_check.py` Stop hook scans transcript for worker Task + merge-shaped Bash + zero reviewer Task; emits `stopReason` advisory. | 003 |
| 2026-05-16 | Promoted CR-INV-007 from convention-partial to convention-with-tooling — `~/.claude/bin/redact_projection.py` portable callable with conservative DEFAULT_PATTERNS; CLI `--strict` exits 11 on pattern hit; importable as `redact()` / `was_redacted()`. | 007 |
