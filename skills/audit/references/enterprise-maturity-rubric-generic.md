---
policy_doc_kind: enterprise_rubric
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names
---

# Enterprise Maturity Rubric (Global Generic Baseline) v1.0

This rubric is a reusable, repo-agnostic baseline for enterprise maturity scoring.
Project-level rubrics may override this baseline when present.

## Scoring Model

### Category Scale
- Score each category from `1` to `5`.
- Interpretation:
  - `1`: ad hoc/unmanaged
  - `2`: partial/inconsistent
  - `3`: defined minimum standard
  - `4`: reliable and repeatable
  - `5`: optimized and continuously verified

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
- `80-89`: Enterprise-Ready
- `90-100`: Enterprise-Mature

## Default Category Set and Weights

Weights total `100`:

| Key | Category | Weight |
|---|---|---:|
| `security` | Security | 14 |
| `api-contracts` | API Contracts and Boundary Validation | 10 |
| `testing` | Test Strategy and Reliability | 10 |
| `data-integrity` | Database and Data Integrity | 10 |
| `observability` | Observability and Traceability | 9 |
| `cicd` | CI/CD and Build Governance | 9 |
| `documentation` | Operational Documentation and Runbooks | 8 |
| `separation` | Separation of Concerns | 7 |
| `clean-code` | Clean Code and Maintainability | 6 |
| `modularity` | Modularity and Extensibility | 6 |
| `error-handling` | Error Handling and Recovery | 6 |
| `type-safety` | Type and Schema Safety | 5 |

## Hard Gates (Diagnostic Keys)

| Key | Name | Severity |
|---|---|---|
| `security-critical-zero` | No critical security vulnerabilities | `critical` |
| `contract-validation-100` | API/route contract validation at required coverage | `high` |
| `critical-domain-floor` | Security/API contracts/testing/data-integrity each >= 3 | `high` |
| `evidence-freshness-30d` | Evidence freshness <= 30 days | `stale` |
| `strict-skip-control-zero` | No skip-control debt in strict lane | `high` |
| `no-destructive-db-default-path` | No destructive DB defaults in executable paths | `critical` |

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
- `overall.rawPercent`, `overall.confidenceWeightedPercent`, `overall.adjustedPercent`, `overall.totalPenalty`, `overall.maturityBand`, `overall.enterpriseMature`, `overall.penalties[]`
- `summary.average`, `summary.min`, `summary.enterpriseReady`, `summary.passingHardGates`
- `hardGates[]`, `enterpriseOverlays[]`

## Adoption Checklist

1. Define category metrics and thresholds.
2. Assign owners for categories and gates.
3. Define evidence artifacts and freshness windows.
4. Encode hard gates with stable keys.
5. Validate determinism with two consecutive identical gate outcomes.
6. Publish remediation priorities from lowest categories and penalties.
