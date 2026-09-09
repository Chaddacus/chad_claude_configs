---
policy_doc_kind: enterprise_design_rubric
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names
---

# Enterprise Design Rubric (Global Generic Baseline) v1.0

This rubric is a reusable, repo-agnostic baseline for scoring product UI and
design quality with evidence instead of taste. Product-level rubrics may overlay
domain, brand, or platform-specific criteria when present.

## Source Anchors

- NN/g 10 Usability Heuristics: https://www.nngroup.com/articles/ten-usability-heuristics/
- NN/g Severity Ratings for Usability Problems: https://www.nngroup.com/articles/how-to-rate-the-severity-of-usability-problems/
- WCAG 2.2: https://www.w3.org/TR/WCAG22/
- Core Web Vitals: https://web.dev/articles/vitals
- USWDS Design Principles: https://designsystem.digital.gov/design-principles/
- USWDS Maturity Model: https://designsystem.digital.gov/maturity-model/

## Scoring Model

### Category Scale

- Score each category from `1` to `5`.
- Interpretation:
  - `1`: ad hoc, visually or functionally unmanaged
  - `2`: partial, inconsistent, or locally good but not system-grade
  - `3`: defined minimum standard with core flows usable and accessible
  - `4`: reliable, repeatable, system-backed, and validated across core states
  - `5`: optimized, continuously measured, polished, and resilient across edge cases

### Composite Scores

- `rawPercent = sum((categoryScore / 5) * categoryWeight)`
- `confidenceWeightedPercent = sum((categoryScore / 5) * categoryWeight * confidenceMultiplier)`
- `adjustedPercent = confidenceWeightedPercent - totalPenalty`

### Confidence Multipliers

- `high = 1.00`
- `medium = 0.90`
- `low = 0.75`

### Penalty Defaults

- `critical`: `-8` each, cap `-24`
- `high`: `-4` each, cap `-16`
- `stale`: `-2` each, cap `-12`

### Maturity Bands

- `0-49`: Foundational
- `50-64`: Developing
- `65-79`: Operational
- `80-89`: Enterprise-Design-Ready
- `90-100`: Enterprise-Design-Mature

## Default Category Set and Weights

Weights total `100`:

| Key | Category | Weight |
|---|---|---:|
| `user-task-fit` | User Needs and Task Outcome Fit | 9 |
| `workflow-clarity` | Information Architecture and Workflow Clarity | 9 |
| `interaction-usability` | Interaction Quality and Usability Heuristics | 12 |
| `accessibility` | Accessibility and Inclusive Design | 14 |
| `visual-hierarchy` | Visual Hierarchy and Aesthetic Discipline | 10 |
| `design-system` | Design System Coherence and Token Discipline | 9 |
| `content-ux` | Content UX, Microcopy, and Terminology | 8 |
| `platform-continuity` | Responsive and Cross-Platform Continuity | 7 |
| `performance` | Perceived Performance and Web Vitals | 7 |
| `trust-safety` | Trust, Safety, Error Prevention, and Recovery | 7 |
| `research-measurement` | Research Evidence and Design Measurement | 5 |
| `implementation-fidelity` | Implementation Fidelity and Maintainability | 3 |

## Judging Guidance

Each category should be scored from current evidence, not reviewer preference.
Acceptable evidence includes screenshots, recorded flows, Playwright traces,
axe/accessibility output, keyboard walkthrough notes, Lighthouse/Web Vitals
output, design-token inventory, visual regression output, component inventory,
heuristic review notes, and user/task validation notes.

Use NN/g-style severity when grading usability findings:

- `severity 0`: not a usability problem
- `severity 1`: cosmetic or minor friction
- `severity 2`: minor problem with a straightforward workaround
- `severity 3`: major problem that slows or confuses important work
- `severity 4`: catastrophe that prevents task completion or causes severe risk

Use WCAG 2.2 as the accessibility baseline. Critical flows should have no known
WCAG 2.2 AA blockers, keyboard-only use should work, focus should be visible,
and status/error messages should be perceivable by assistive technology.

Use Core Web Vitals when the surface is a web UI. For critical screens, the
preferred floor is `LCP <= 2.5s`, `INP <= 200ms`, and `CLS <= 0.1` at the 75th
percentile, or a documented local equivalent when field data is unavailable.

## Hard Gates (Diagnostic Keys)

| Key | Name | Severity |
|---|---|---|
| `critical-flow-completable` | All named critical flows are completable without dead ends or hidden required actions | `critical` |
| `wcag-aa-critical-path` | Critical flows have no known WCAG 2.2 AA blockers; keyboard-only use and visible focus work | `critical` |
| `severity-4-usability-zero` | No NN/g-style severity 4 usability issue remains in a critical flow | `critical` |
| `destructive-action-clarity` | Destructive or high-risk actions have clear labels, consequences, and recovery or confirmation behavior | `high` |
| `performance-floor` | Critical web screens meet the Core Web Vitals floor or document a local equivalent | `high` |
| `system-token-floor` | Core UI uses named design tokens or documented variables for color, type, spacing, and states | `high` |
| `evidence-freshness-30d` | Evidence freshness <= 30 days unless explicitly marked stale | `stale` |

## Evidence Contract

Each category should emit:

- `score`
- `confidence`
- `owner`
- `evidenceFreshnessDays`
- `topRisks[]`
- `metrics`

Freshness defaults:

- General evidence: `<= 30 days`
- Drill evidence: `<= 90 days`

## Output Contract

Recommended scorecard fields:

- `rubricVersion`, `generatedAt`, `branch`, `commit`, `mode`
- `categories[]`, `categoryWeights`
- `overall.rawPercent`, `overall.confidenceWeightedPercent`, `overall.adjustedPercent`, `overall.totalPenalty`, `overall.maturityBand`, `overall.enterpriseDesignMature`, `overall.penalties[]`
- `summary.average`, `summary.min`, `summary.enterpriseDesignReady`, `summary.passingHardGates`
- `hardGates[]`, `sourceAnchors[]`, `designEvidence[]`

## Adoption Checklist

1. Define critical flows and user tasks before scoring.
2. Define category metrics and thresholds.
3. Assign owners for categories and gates.
4. Define evidence artifacts and freshness windows.
5. Encode hard gates with stable keys.
6. Validate determinism with two consecutive identical gate outcomes.
7. Publish remediation priorities from lowest categories and penalties.

