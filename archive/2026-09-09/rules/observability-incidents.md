# Observability, Incidents & Bounded Self-Healing (SPEC.md Standard 7)

## Observability contract

OpenTelemetry is the instrumentation contract; Elastic/Kibana is the operational platform. Every production application is traceable through correlated traces, structured logs, and metrics carrying: environment, service/module, release/commit/artifact, and request/trace identifiers (field contract: `templates/observability/otel-correlation.md`). Health checks, FAST synthetic verification, and observability are distinct things — do not conflate them. Production apps define SLIs/SLOs or explicit reliability thresholds proportional to criticality.

## Incidents

- **Elastic Cases are the canonical live incident source of truth.** GitHub issues/PRs track remediation and link back to the Case. Ground incident state from the Case and live telemetry, never from memory or a stale handoff.
- Severity: SEV-0 (broad outage/compromise/data-integrity risk) · SEV-1 (major customer-facing degradation) · SEV-2 (limited impact/workaround exists) · SEV-3 (minor). **Severity changes urgency, not authority.** SEV-0/1: notify the user promptly after grounding impact, then continue all permitted autonomous response.
- During severe impact, restore safe service before pursuing perfect root cause when a safe mitigation exists.
- Parallel diagnosis is hypothesis-based: workers investigate distinct hypotheses and return supporting AND contradicting evidence.
- Every production incident action stays traceable: alert → Case → session → GitHub remediation → verification → approvals → deployment → live result.

## Self-healing authority ladder

- **L1 Detect / L2 Diagnose:** autonomous.
- **L3 Remediate to DEV:** autonomous — implement, review, merge/deploy DEV, fully verify the repair.
- **L4 Bounded production healing:** autonomous ONLY through a tested, pre-authorized runbook (contract: `templates/runbooks/runbook-schema.yaml`) with exact triggers, bounds, verification, abort, and audit. Runbooks are tested in DEV/staging/simulation before production autonomy is enabled. **New code always requires the two human gates** — no runbook deploys new code.

## Learning

Postmortems: SEV-0 mandatory; SEV-1 normally mandatory; SEV-2 when novel/recurring/high-learning; SEV-3 only on aggregate impact. Production escapes become regression tests/evals when practical.
