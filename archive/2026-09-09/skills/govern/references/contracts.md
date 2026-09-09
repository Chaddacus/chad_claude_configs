# Govern Skill — Output Contracts

## Route Classification Output

Schema returned by `classify_route.py`:

```json
{
  "route_id": "R3",
  "route_name": "non_trivial_impl",
  "execution_shape": "bounded_swarm",
  "profile_overrides": {
    "planner": { "model": "claude-opus-4-6", "effort": "high" },
    "worker": { "model": "claude-opus-4-6", "effort": "medium" },
    "explorer": { "model": "claude-haiku-4-5", "effort": "low" },
    "validator": { "model": "claude-haiku-4-5", "effort": "medium" },
    "reviewer": { "model": "claude-opus-4-6", "effort": "high" }
  },
  "lane_caps": {
    "planner": 1, "explorer": 2, "worker": 2,
    "validator": 2, "reviewer": 1
  },
  "frontier_dispatch_order": ["validator", "explorer", "worker", "reviewer"],
  "reviewer_barrier_points": ["closure", "high_risk_boundary_shrink"],
  "swarm_cap": 4,
  "convergence_required": true,
  "risk_class": "medium",
  "packetization_required": true,
  "default_parallelism_policy": "bounded_parallel"
}
```

## Task Characteristics Input

Schema accepted by `classify_route.py` on stdin:

```json
{
  "file_count_estimate": 5,
  "touches_auth": false,
  "touches_security": false,
  "touches_migrations": false,
  "touches_production_behavior": true,
  "estimated_complexity": "moderate",
  "has_ambiguity": false
}
```

### Field definitions

| Field | Type | Description |
|---|---|---|
| `file_count_estimate` | int | Number of files expected to change |
| `touches_auth` | bool | Changes authentication or authorization logic |
| `touches_security` | bool | Changes security-sensitive code |
| `touches_migrations` | bool | Changes database schema or data migrations |
| `touches_production_behavior` | bool | Changes observable production behavior |
| `estimated_complexity` | string | One of: trivial, simple, moderate, complex |
| `has_ambiguity` | bool | Intent cannot be fully resolved from context |

### Complexity mapping

| Value | Meaning |
|---|---|
| `trivial` | Single line change, typo fix, config tweak |
| `simple` | Bounded change to 1-2 files, clear intent |
| `moderate` | Multi-file change, some design decisions required |
| `complex` | Architectural change, cross-cutting concerns, risk factors |

## Team Spec Output

Schema returned by `build_team_spec.py`:

```json
{
  "team_name": "govern-a1b2c3d4",
  "execution_mode": "bounded_swarm",
  "route_id": "R3",
  "swarm_cap": 4,
  "lane_caps": { "planner": 1, "explorer": 2, "worker": 2, "validator": 2, "reviewer": 1 },
  "frontier_dispatch_order": ["validator", "explorer", "worker", "reviewer"],
  "reviewer_barrier_points": ["closure", "high_risk_boundary_shrink"],
  "convergence_required": true,
  "members": [
    {
      "name": "planner",
      "agentType": "Plan",
      "model": "claude-opus-4-6",
      "role": "planner",
      "prompt": "--- (full agent definition) ---",
      "sandbox": "read-only",
      "effort": "high"
    }
  ]
}
```

For R1/R2/R5 (inline execution):

```json
{
  "execution_mode": "inline",
  "route_id": "R2",
  "team_name": null,
  "members": []
}
```

## Prompt Classification Output

Schema returned by `classify_prompt.py` (hook output):

```json
{
  "route_hint": "R3",
  "governance_recommended": true,
  "reason": "non-trivial (5 files mentioned)"
}
```

## Session Startup Output

Schema returned by `session_startup.py` (hook output):

```json
{
  "governance_ready": true,
  "manifest": { "valid": true, "message": "ok" },
  "agents": { "all_present": true, "details": [] },
  "lock_dir": { "path": "/path/to/locks", "exists": true, "writable": true },
  "runtime_files": { "all_present": true, "checked": 6, "missing": [] }
}
```

## Governed Closure Vocabulary

| State | Meaning | Closure Type |
|---|---|---|
| `OBJECTIVE_COMPLETE` | All packets accepted, convergence confirmed, postflight approved | Strong |
| `OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK` | Completed with reduced scope | Weak |
| `OBJECTIVE_BLOCKED_ESCALATION_REQUIRED` | Cannot proceed without authority decision | Blocked |
| `OBJECTIVE_BLOCKED_MIGRATION_DEFECT` | Infrastructure incompatibility | Blocked |
| `OBJECTIVE_REJECTED_FALSE_COMPLETION` | Closure claim unsupported by evidence | Re-enter |

## Packet Task Contract

When creating tasks for team members, each task description must include:

```
## Packet: <packet_id>
Primary behavior: <single behavior description>
Allowed scope: <files/modules that may be modified>
Dependencies: <list of packet_ids that must complete first>
Dependency mode: accepted_upstream | explicit_stub

## Acceptance Checks
- [ ] <specific, verifiable check>
- [ ] <specific, verifiable check>

## Failure Signals
- <what indicates this packet has failed>

## Constraints
- <scope boundaries, prohibited actions>
```

## Reviewer Barrier Contract

At reviewer barrier points, the reviewer task must include:

```
## Review Scope
Barrier type: <closure | high_risk_boundary_shrink | adaptation_generated_packets>
Packets under review: <list of packet_ids>

## Required Evaluation
- Correctness: do implementations match acceptance checks?
- Regressions: any broken existing behavior?
- Security: any new vulnerabilities introduced?
- Evidence quality: are completion claims backed by test results / verifier verdicts?
- Solution layer: was the highest useful layer chosen?

## Verdict
APPROVE | REJECT with rework instructions
```
