# AI / LLM Application Engineering (SPEC.md Standard 9)

## Governing rule

**An LLM is a probabilistic dependency behind a deterministic application contract.** Deterministic software owns state, authorization, business validation, critical calculations, and irreversible side effects. AI handles only justified ambiguity: language, reasoning, synthesis, extraction, planning, bounded tool selection.

## Capability contract

Every AI feature has a capability contract before DEV (template: `templates/evals/ai-capability-contract.md`): owner/module, purpose, input/output contracts, grounding sources, allowed/forbidden tools, authority model, provider/model policy, latency/cost/iteration budgets, fallback behavior, eval suite + thresholds, observability.

## Provider strategy

Claude-first: implement with Claude unless a project has a concrete reason otherwise; add a second provider later as an explicit follow-on. Business modules never scatter provider SDK calls — route through a gateway/capability boundary so a later provider needs no business-logic rewrite. Do not build or test hypothetical providers.

## Versioned behavior

Prompt, model, effort, retrieval, chunking, embedding/ranking, tool schema/description, memory-policy, routing, and fallback changes are **behavioral software changes** — affected eval suites run before DEV promotion.

## Structured output and tools

- Software-consumed AI output uses schema-constrained structured output where available; schema validity is followed by normal deterministic validation and authorization. Valid JSON is not semantic correctness.
- Tools expose bounded business capabilities, never arbitrary shell/SQL/admin. Each tool declares purpose, schemas, side-effect class, idempotency, authorization, data classification, timeout/retry, and approval requirement. **Authorization is enforced at execution time by the application, not by prompt instructions.**
- Architecture ladder — use the simplest that passes the evals: deterministic code → one call → +retrieval → +bounded tools → agent loop → multi-agent (only when independence genuinely justifies coordination cost).

## RAG and memory

RAG preserves source identity, version, authority/freshness, permissions, and classification; authorization filters apply BEFORE sensitive content enters context; similarity never outranks canonical/current sources. Memory is non-authoritative unless explicitly designed otherwise, with provenance/expiry where consequential.

## Mandatory eval policy

**If an AI capability is not evaluated, assume it is broken.** Every AI capability has an eval suite before DEV; no suite, failing suite, or stale suite = NOT READY — fail closed (`verify-module` enforces this). Suites cover normal/edge/regression/malformed/adversarial-injection/tool-boundary/grounding/structured-output/fallback/latency-cost cases as relevant (template: `templates/evals/eval-suite.md`). Prefer deterministic graders; independent LLM graders only for genuinely subjective criteria. Production AI failures become regression evals when practical.

## AI verification tiers and observability

FAST = provider reachable + structured-output basics + golden-case heartbeat. MODULE = full capability eval suite + adversarial/cost/latency. FULL = cross-capability workflows. Manifests record provider/model/config, prompt version, eval dataset/grader versions, thresholds/results, retrieval/toolset version. Instrument AI operations through OTel/Elastic; record metadata (provider, capability, latency, tokens/cost, tools, outcome) — never raw prompts/responses/confidential retrievals by default.
