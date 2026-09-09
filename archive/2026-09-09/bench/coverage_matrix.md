# Coverage matrix — architecture claim → fixture

Every claim the stage-aware-orchestrator-loop architecture makes is paired
with at least one fixture in `~/.claude/policy/fixtures/phase_loop_corpus.jsonl`
that fails if the claim breaks. Linted by `analyze.py coverage-matrix`.

Amendment protocol: adding a new architecture claim requires adding a new
fixture in the same PR. Removing a claim requires a separate ADR.

| Architecture claim | Fixture task_id |
|---|---|
| R1 lookup work bypasses phase machinery entirely | r1-lookup-1 |
| R1 lookup bypass is consistent across prompt shapes | r1-lookup-2 |
| R2 small implementation runs minimal machinery (build→verify→closeout only) | r2-impl-1 |
| R2 surgical fix in a single named file doesn't re-question owned_files | r2-impl-2 |
| R2 trivial rename produces near-bypass; verifier matrix doesn't slow it (H-β) | r2-impl-3 |
| R2 minimal-touch file creation has verifier classify as not_applicable | r2-impl-4 |
| R3 non-trivial impl exercises full phase machinery (discovery→…→closeout) | r3-impl-1 |
| R3 refactor across multiple call sites runs grep-then-edit cleanly | r3-impl-2 |
| R3 self-validation: recursive validation of Slice 1b's own work | r3-impl-3 |
| R4 auth work records threat_model + security_review evidence | r4-auth-1 |
| R4 migration records concurrent-write analysis + rollback plan | r4-migration-1 |
| R4 billing change preserves audit trail + edge-case test coverage | r4-billing-1 |
| R5 ambiguous prompt stays in discovery until clarified, then promotes R5→R3 | r5-to-r3-1 |
| Owned_files question produces changed=false when file already named in prompt (neg-control) | neg-1 |
| Doc-only change produces changed=false on owned_files and next_action (neg-control) | neg-2 |
