# Policy-pack selection decision tree

Use this when the user doesn't specify `--pack`.

```
User ask mentions…
├── SOC 2 / SOC2 / "Type II"                       → soc2
├── HIPAA / PHI / Security Rule                    → hipaa
├── PCI / PCI DSS / cardholder data                → pci
├── ISO 27001 / Annex A                            → iso-27001
├── NIST / CSF / cybersecurity framework           → nist-csf
├── "client engagement" / "for <customer>" / CW    → cw-internal
└── (none of the above)                            → default
```

Additional heuristics (apply after the tree):

- If the target imports `anthropic`, `openai`, LangChain, LlamaIndex, or declares MCP servers → always pass `--profile ai-app` regardless of pack.
- If the target is openshield itself (`packages/rubric` exists) → pass `--suppressions /Users/chadsimon/code/openshield/openshield-suppressions.json` to avoid analyzer-on-analyzer false positives.
- If the user says "strict" or "gate" → add `--fail-on-strict --fail-on-band operational`.

## Pack characteristics at a glance

| Pack | AppSec weight | AI weight | Strict gates | Freshness | When |
|---|---:|---:|---|---|---|
| `default` | 60 | 40 | critical-zero + 3 category floors + freshness | 30d | Neutral baseline |
| `soc2` | 60 | 40 | default + overall>=70 + appsec>=70 | 30d | SOC 2 readiness |
| `hipaa` | 60 | 40 | default + no-phi-secrets + authz>=4 + overall>=75 | 30d | HIPAA readiness |
| `pci` | 60 | 40 | default + no-secrets + injection>=4 + authz>=4 + dep>=3 + overall>=80 | 30d | PCI readiness |
| `iso-27001` | 60 | 40 | default + secure-dev>=4 + overall>=75 | 30d | ISO readiness |
| `nist-csf` | 60 | 40 | default + pr-aa>=3 + pr-ps>=3 + overall>=70 | 30d | CSF target profile |
| `cw-internal` | 45 | 55 | default + no-high-or-critical + ai-track>=80 | **14d** | CW consulting deliverable |

## Compliance disclaimer

Every compliance pack is a **readiness indicator, not a legal certification**. The `compliance_mode` field in the pack metadata says `readiness_only`. When presenting a score on any compliance pack, include a single sentence noting this — e.g. "This is a source-level readiness indicator, not a QSA/auditor sign-off."
