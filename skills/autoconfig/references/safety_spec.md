# Autoconfig Safety Specification

Safety guardrails for configuration mutation. Every mutation path in the autoconfig skill
must respect these constraints. Violations are treated as hard failures.

---

## Immutable Fields (never mutated)

These fields are excluded from the mutation search space unconditionally.

| Field | Reason |
|---|---|
| `permissions.deny` | Safety boundary. Controls what Claude is forbidden from doing. Mutating this could grant dangerous capabilities (file deletion, network access, secret exfiltration). |
| `permissions.allow` | Safety boundary. Controls what Claude is explicitly permitted to do. Weakening or expanding this bypasses the user's trust contract. |
| `env.CLAUDE_HOME` | Runtime identity. Every path resolution in the agent stack derives from this value. Changing it disconnects the running session from its config, memory, skills, and state. |
| `mcpServers` | External integration surface. MCP server entries bind Claude to external tools (chatroom, IDE, cloudwarriors). Mutating connection strings or removing entries breaks live integrations with no rollback path. |
| `hooks` | Governance enforcement. Hooks drive `UserPromptSubmit`, `SessionStart`, `Stop`, and other lifecycle events. Disabling or rewriting hooks removes safety nets, notification contracts, and classification gates. |
| `control_plane_ref` | Structural pointer. References the extracted control plane config at `skills/planning-gate/references/control_plane.json`. Changing the pointer breaks all runtime validation, scheduling, and artifact contracts. |
| `postflight.enabled` | Enforcement control. Disabling postflight removes the final quality gate that catches regressions, missing tests, and incomplete delivery. |
| `postflight.mode` | Enforcement control. Changing the mode (e.g., from `blocking` to `advisory`) weakens the gate from a hard stop to a suggestion, defeating its purpose. |
| `rules.*.risk_class` | Route safety classification. Risk class determines whether work routes through R1 (fast) or R3/R4 (bounded swarm with reviewer barriers). Lowering a risk class could send security-sensitive work through an ungoverned fast lane. |
| `thresholds.high_risk_false_negatives` | Zero-tolerance threshold. This value must stay at 0. Any nonzero value means the system would accept false negatives on high-risk classification, allowing dangerous work to bypass governance. |

---

## Mutation Bounds

All mutable numeric and enum fields are clamped to these ranges. Values outside bounds
are rejected before benchmarking.

| Parameter | Range | Rationale |
|---|---|---|
| Lane caps | [1, 4] | Below 1 means no agents can execute. Above 4 means unbounded contention for shared resources (filesystem, git, network). |
| Swarm cap | [1, 8] | Above 8 means runaway resource consumption. Each swarm member holds context, spawns processes, and competes for model API quota. |
| Max parallel packets | [1, 6] | Above 6 means scheduling becomes chaotic. Packet dependencies form DAGs; too many in-flight packets cause starvation, deadlocks, and reviewer bottlenecks. |
| Effort level | `{low, medium, high}` | Only valid enum values. Controls how much exploration and verification agents perform. Invalid values cause runtime errors. |
| Model | `{claude-haiku-4-5, claude-sonnet-4-6, claude-opus-4-6}` | Only currently available models. Using an unavailable model causes API failures. Model selection affects cost, latency, and reasoning quality. |

---

## Session Isolation Rules

Benchmarks must never interfere with the user's live session or persist state across runs.

1. **Flag isolation**: All benchmark processes run with `--no-session-persistence --dangerously-skip-permissions` to prevent state leakage and avoid interactive permission prompts.
2. **Workspace isolation**: Each benchmark runs in an isolated temp workspace (`mktemp -d`). No benchmark reads from or writes to the user's real project directories.
3. **Environment marking**: The `AUTOCONFIG_BENCHMARK=1` environment variable is set on every benchmark process. This allows hooks, scripts, and other infrastructure to detect and skip benchmark-only execution.
4. **Cleanup discipline**: All temp workspaces, spawned processes, and intermediate artifacts are cleaned up in `finally` blocks. Benchmark failures must not leave orphaned directories or zombie processes.

---

## Conflict Avoidance Rules

Autoconfig must not run while the user has an active Claude session, to prevent
config corruption, file contention, and confusing behavior.

1. **Lock check**: Before starting, check `~/.claude/state/locks/` for active session lock files. A lock file with `mtime < 10 minutes` indicates a live session.
2. **Process check**: Check for interactive Claude processes via `pgrep`. Any running Claude CLI process is treated as a potential conflict.
3. **Clean exit on conflict**: If either check detects a conflict, exit cleanly with a descriptive message. Do not attempt to force-acquire locks or kill existing processes.
4. **Retry policy**: After a clean exit due to conflict, retry after a 2-minute delay. This gives transient sessions time to complete without requiring user intervention.
