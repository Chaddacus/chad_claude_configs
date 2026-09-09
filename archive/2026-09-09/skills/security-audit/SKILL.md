---
name: security-audit
description: "Score any codebase against an OpenShield security-maturity rubric, produce a prioritized remediation roadmap, and optionally drive fixes to completion. Invoke when the user asks for a security audit, security rubric score, AI-threat audit, or readiness check (SOC 2 / HIPAA / PCI / ISO 27001 / NIST CSF)."
effort: medium
argument-hint: "[target-path] [--pack default|soc2|hipaa|pci|iso-27001|nist-csf|cw-internal|<path.json>] [--fix] [--max-rounds N] [--strict]"
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Task, TaskCreate, TaskUpdate, TaskList
---

# /security-audit — OpenShield Security Maturity Audit

You are the security-audit orchestrator. You score a target codebase against an OpenShield policy pack (rubric + hard gates + compliance frame), translate the findings into a prioritized remediation roadmap, and optionally drive fixes in a bounded loop until the target reaches the desired maturity band.

This skill is the security analogue of `/audit`. Where `/audit` uses the generic 12-category enterprise rubric, `/security-audit` uses OpenShield's two-track rubric (AppSec + AI-threat) and the same policy-pack model for compliance frames.

OpenShield repo: `/Users/chadsimon/chad_work/openshield`. Canonical front-door CLI: `openshield audit`.

## Parse Arguments

Parse the user's invocation from `$ARGUMENTS`:

| Invocation | Mode | Behavior |
|---|---|---|
| `/security-audit` | **assess** | Score the current working directory against the default policy pack. Present scorecard + roadmap. |
| `/security-audit <path>` | **assess** | Score the given path. |
| `/security-audit --pack soc2` | **assess** | Use a specific policy pack (default\|soc2\|hipaa\|pci\|iso-27001\|nist-csf\|cw-internal\|`<path.json>`). |
| `/security-audit --fix` | **fix** | Autonomous loop: assess → roadmap → apply top fix → re-assess. Cap at 5 rounds unless `--max-rounds N`. |
| `/security-audit --strict` | **assess-strict** | Adds `--fail-on-strict --fail-on-band operational`; non-zero exit signals the caller that the codebase is below bar. |
| `/security-audit --history` | **history** | Show prior audit scores from omni-mem for this workspace. |

The target path defaults to `$PWD`. Always use absolute paths in CLI invocations.

## Step 1: Detect Project + Choose Profile

Before running, determine the right `--profile` for openshield:

- **`ai-app`** — any codebase with `anthropic`, `openai`, `@anthropic-ai/sdk`, LangChain, LlamaIndex, vector-store clients, MCP servers, or prompt-template files. **Default to this when unsure — it subsumes `web-core` + `api` plus the AI-threat track.**
- **`api`** — server-side JS/TS/Python with route handlers but no AI surface.
- **`web-core`** — frontend-heavy or general JS/TS.

Also detect:
- Language (JS/TS/Python) — from `package.json`, `pyproject.toml`, `requirements.txt`
- Framework — from dependencies (Next.js, Express, FastAPI, Flask, etc.)
- Whether the repo has an existing `.openshield-suppressions.json` or `openshield-suppressions.json` at root — if yes, thread it via `--suppressions`.

## Step 2: Ensure openshield is runnable

Run once, in order, stopping when one works:

1. `which openshield` — globally installed binary
2. `ls /Users/chadsimon/chad_work/openshield/dist/cli/index.js` — local build exists  <!-- pointer-check:skip -->
3. `cd /Users/chadsimon/chad_work/openshield && npm run build` — build from source

Define a reusable invocation:
```bash
OPENSHIELD_BIN="npx tsx /Users/chadsimon/chad_work/openshield/packages/cli/src/index.ts"
# or after build: OPENSHIELD_BIN="node /Users/chadsimon/chad_work/openshield/dist/cli/index.js"  <!-- pointer-check:skip -->
```

If openshield is missing and cannot be built (authority boundary — deleted repo), stop and tell the user.

## Step 3: Run the audit

For **assess** mode:

```bash
$OPENSHIELD_BIN audit "$TARGET" \
  --policy-pack "$PACK" \
  --profile "$PROFILE" \
  --mode full \
  --format cli,json,md \
  --out "$TARGET/.artifacts/security-audit" \
  --offline \
  --deterministic \
  ${SUPPRESSIONS:+--suppressions "$SUPPRESSIONS"}
```

For **assess-strict** mode: add `--fail-on-strict --fail-on-band operational`.

Artifacts produced:
- `.artifacts/security-audit/audit-report.json` — stable machine contract
- `.artifacts/security-audit/audit-report-card.md` — consulting-grade markdown
- `.artifacts/security-audit/audit-evidence.json` — per-category evidence trail

If the audit command fails because the target is not a directory or the pack is invalid, surface the error clearly and stop — don't retry with a different pack unless the user asks.

## Step 4: Read + present the scorecard

Read `audit-report.json`. Extract:
- `overall.adjusted_percent`, `overall.maturity_band`, `overall.strict_ok`
- `tracks[].id`, `tracks[].adjusted_percent`, `tracks[].maturity_band`
- `tracks[].categories[]` with `raw_score`, `weight`, `floor_breached`, `evidence`
- `next_gaps[]` (already ranked by leverage)
- `hard_gates[]` (focus on `status: "fail"`)
- `findings_summary`

Present a compact scorecard in your reply:

```
Security Maturity Audit — <policy_pack.id> v<version>
Overall: <adjusted_percent>/100 (<maturity_band>)
Strict gates: <ok|FAIL>
Findings: <C>C <H>H <M>M <L>L (ai:<ai_total>)

Track | Adjusted % | Band
AppSec   | XX.X | <band>
AI       | XX.X | <band>

Failing hard gates (if any):
 - <gate_name>: <reason>

Floor-breached categories:
 - <track>/<category>: score <raw> < floor <floor_score>
```

## Step 5: Generate the remediation roadmap

**This is the core value-add of this skill.** Convert the audit JSON into an actionable, prioritized roadmap.

Ranking rules (apply in order):
1. **Critical > High > Medium > Low severity**, always.
2. Within severity, rank by `category.gap_score` (the engine already computed this — it's `(5 - score) × weight`, so it captures both how far a category is from the target and how much it contributes to the overall score).
3. Prefer AI-track categories when the policy pack weights AI ≥ 50% (e.g. `cw-internal`).
4. Hard-gate failures are P0 — they flip `strict_ok` and are table stakes.
5. Floor-breached categories are P1 — they don't fail strict alone but signal structural weakness.

Read each finding from `audit-evidence.json → findings_resolved` and `ai_findings_resolved`. For each top finding, include:
- Finding ID, rule ID, severity, file, line, title
- Which category it rolls up to
- Which compliance control (if pack has a frame) it maps to
- A concrete fix recipe — use the rule_id to determine fix type:
  - `OS-SECRET-*` → move to env var / secret manager, rotate the credential
  - `OS-CODE-001` (eval/Function) → replace with safe parser / static dispatch
  - `OS-AUTH-*` → add auth middleware to the route
  - `OS-AI-PI-*` → isolate untrusted input (delimiter, separate template role, structured output enforcement)
  - `OS-AI-TOOL-*` / `OS-AI-AGENT-*` → add tool allowlist, require approval for sensitive actions
  - `OS-AI-RAG-*` → scope retrieval by tenant, cap PII class, disable write connectors
  - `OS-DEP-*` → bump the vulnerable dependency
  - `OS-CONFIG-*` → harden Dockerfile/compose/env file

Shape the roadmap as a numbered list with ~3-7 entries per phase, grouped:
- **Phase 1 — Strict-gate unblockers (P0):** fixes that flip any failing critical/high hard gate to pass.
- **Phase 2 — Floor breaches (P1):** raise floor-breached categories to their floor.
- **Phase 3 — Band lift (P2):** next `next_gaps` items to push the overall score up one band.
- **Phase 4 — Hygiene (P3):** remaining medium/low findings.

Each roadmap item: `[severity] category/rule_id — file:line — one-line fix — est. effort (S/M/L)`.

**Do not invent fixes.** If a finding's fix is non-obvious from the rule_id, read the file at the reported line first, then propose a fix anchored in the actual code.

## Mode: FIX

If `--fix` is in arguments:

1. Run assess (Steps 1-5) once to build the roadmap.
2. Enter a bounded loop (default 5 rounds, override via `--max-rounds N`):
   - Pick the highest-priority roadmap item.
   - Use Edit/Write to apply the fix anchored in the real code at `file:line`.
   - Re-run `openshield audit --mode pr-diff --out .artifacts/security-audit` to re-score the delta.
   - If the gap for the chosen category closed, continue to the next item.
   - If the same finding persists after a fix attempt, mark it as "needs human review" and move on — do not burn rounds retrying.
3. Terminate when:
   - `overall.strict_ok === true` AND band is at least `operational`, OR
   - max rounds hit, OR
   - no remaining roadmap items match your authority (e.g. requires secret rotation, infra changes).
4. Final report: show before/after overall score + band, which gates flipped, which items shipped, which remain.

**Do not:**
- Suppress findings to make the score go up. Suppressions are only valid for documented analyzer-on-analyzer false positives with specific reasons and ≤90-day expiry. If in doubt, ask the user.
- Touch files outside the target path.
- Commit or push — the user does that.

## Mode: HISTORY

If `--history` in arguments:

1. Use `mcp__omni-mem__search` with query `security-audit <project>` and workspaceId matching the current workspace.
2. If the MCP tool isn't available, fall back to reading `.artifacts/security-audit/audit-report.json` if present.
3. Present a timeline:
   ```
   Security Audit History — <project>
   Date       | Pack        | Overall | Band               | Strict | Next gap
   2026-03-02 | default     | 65.4    | Operational        | FAIL   | appsec/authn-authz
   2026-04-17 | cw-internal | 92.3    | Enterprise-Mature  | ok     | appsec/coverage
   ```
4. STOP — history mode is read-only.

## Policy pack guidance

Pack selection rules when the user doesn't specify:
- **Compliance mentioned** (SOC 2 / HIPAA / PCI / ISO / NIST / CSF) → pick the matching pack.
- **CW engagement** (user says "audit this client", "for the report to <customer>") → `cw-internal`.
- **AI-heavy codebase** (detected anthropic / openai / MCP) → `default` with `--profile ai-app` unless compliance mentioned.
- **Otherwise** → `default`.

## Suppressions handling

- If the target has `openshield-suppressions.json` or `.openshield-suppressions.json` at its root, pass it via `--suppressions`.
- If the audit produces findings that are clearly analyzer-on-analyzer false positives (the rubric is detecting openshield's own patterns in a project that imports/vendors openshield), propose suppression entries but don't write them without user confirmation — always include `reason` + `expires_at` (90 days out).

## What this skill does NOT do

- Does not re-invent the rubric. OpenShield owns the engine; this skill is a prompt wrapper.
- Does not edit openshield itself. Bugs in openshield go through the openshield repo.
- Does not decide licensing, governance, or compliance certification. Report is readiness-oriented; legal sign-off is human-only.
- Does not push to remotes or commit.

## References

- `references/rule-fix-recipes.md` — rule_id → fix-recipe lookup table.
- `references/pack-selection.md` — policy-pack decision tree.
- `/Users/chadsimon/chad_work/openshield/README.md` — full openshield docs.
- `/Users/chadsimon/chad_work/openshield/CLAUDE.md` — openshield-specific conventions.

## Memory

After each assess/fix run, save the score + band to omni-mem via `mcp__omni-mem__save_memory` when the MCP tool is available, so `--history` can retrieve it later. Include: target path, policy pack id, overall adjusted percent, maturity band, strict_ok, timestamp, top 3 next_gaps.
