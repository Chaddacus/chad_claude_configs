---
name: gateway-consolidation
description: Governs all work on the gateway consolidation project (~/gateway_consolidation). Use this skill BEFORE any implementation, testing, or investigation work on the consolidated gateway. It defines the task lifecycle, shadow comparison procedures, and the work standards that leadership can audit.
---

# Gateway Consolidation

## Overview

This skill governs the consolidation of 5 per-vendor platform gateways (Zoom, RingCentral, GoTo, Dialpad, Teams) into one FastAPI service. Every piece of work follows a defined lifecycle: scope, plan, execute, verify, report.

## Task Lifecycle

Every unit of work follows these stages. No stage may be skipped.

### 1. Scope

Define the work in the project repo at `docs/tasks/` as a markdown file with this structure:

```
# TASK-{number}: {title}

**Status:** Proposed | In Progress | Blocked | Complete
**Owner:** {who is doing it}
**Priority:** P0 Critical | P1 High | P2 Medium | P3 Low
**Estimated effort:** {hours or t-shirt size}

## Business context
Why this matters to leadership. One paragraph, plain language.

## Objective
What done looks like. Specific, measurable.

## Acceptance criteria
- [ ] Criterion 1
- [ ] Criterion 2

## Dependencies
What must be true before this starts.

## Risks
What could go wrong, and the mitigation.
```

### 2. Plan

Break the task into steps. Each step has an expected outcome. Write the plan into the task file under a `## Plan` section. Plans are hypotheses — update them when reality changes.

### 3. Execute

Do the work. Commit after each meaningful step. Reference the task number in commits.

### 4. Verify

Run the verification appropriate to the change:
- Code changes: lint + tests (`ruff check src/ tests/` + `pytest tests/`)
- Shadow comparisons: run `scripts/shadow_compare.py` and capture output
- Deployments: health check + smoke test on noob-root

### 5. Report

Update the task file with results, evidence, and status. Update `docs/tasks/INDEX.md` (the leadership-readable task board).

## Shadow Comparison Procedure

Shadow comparison validates that the consolidated gateway returns identical data to the legacy platform gateways for the same request.

### What to compare

For each vendor with active credentials:

1. **Auth** — can the consolidated gateway connect and obtain a session?
2. **Read operations** — every GET endpoint that rapture-bypass calls. Hit both gateways, compare response status and key data fields.
3. **Counts** — the extraction counts endpoint, cross-validated against direct vendor API.
4. **Proxy routes** — the catch-all proxy, comparing response body and status code.

### How to compare

Use the `scripts/shadow_compare.py` script in the project repo. It connects to both gateways, runs defined test cases per vendor, compares response status and key data fields, and outputs a pass/fail report with timing data.

### What needs active tokens

| Vendor | Token source | Status check |
|--------|-------------|-------------|
| Zoom | Legacy pbx_token in Redis DB 0 | HGET on the pbx_token key |
| RingCentral | Legacy system:ringcentral in Redis DB 0 | Check token_expiry field |
| GoTo | Browser-captured via capture-broker | Check credentials key exists |
| Dialpad | Browser-captured via capture-broker | Check system:dialpad keys |
| Teams | Certificate auth via container env vars | Teams gateway container env |

### Path mapping differences

The consolidated gateway and legacy gateways use different URL structures for non-Zoom vendors. Zoom PBX, Phone, CP, CC paths are identical between both gateways. RingCentral, GoTo, Dialpad, and Teams legacy gateways have typed routes while the consolidated gateway uses raw vendor API paths. The shadow comparison script handles this translation.

## Deployment Procedure

No git on noob-root. Deployments use rsync from local, Docker build, and container restart. Always verify health check and middleware auth after deployment.

## Resources

### scripts/
Reserved for governance scripts (shadow comparison automation, task generation).

### references/
Reserved for detailed reference material (vendor auth matrix, deployment checklist).
