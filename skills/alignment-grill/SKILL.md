---
name: alignment-grill
description: Use before implementation when non-trivial product, feature, workflow, UI, architecture, or agent-runtime work is underspecified and Claude needs to establish shared intent, constraints, module/interface impact, vertical slices, acceptance checks, and verification before planning or coding.
---

# Alignment Grill

Use before broad execution, not after implementation starts.

## Workflow

1. Discover repo facts first: read relevant docs, entrypoints, schemas, tests, and existing modules before asking the user anything discoverable.
2. Ask only blocking product or authority questions that cannot be answered from local evidence.
3. Walk the design tree until these are explicit:
   - goal and non-goals
   - target users or operators
   - constraints, risks, and authority boundaries
   - affected modules/interfaces
   - vertical-slice candidates
   - acceptance checks
   - verification commands or manual QA needs
4. Convert the aligned result into either direct slice instructions or a PRD/task-file shaped artifact.

## Output Contract

Before coding, produce a compact alignment summary with:
- `Goal`
- `Non-goals`
- `Constraints`
- `Module/interface impact`
- `Vertical slices`
- `Acceptance checks`
- `Verification plan`
- `Open blockers`

If no blockers remain, continue into implementation using the smallest vertical slice first.
