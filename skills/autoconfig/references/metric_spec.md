# Autoconfig Metric Specification

Defines the composite scoring formula used to evaluate configuration mutations.
A mutation is kept only if its composite score exceeds the current baseline by more
than the noise band threshold.

---

## Composite Score

```
composite = (quality * 0.75) + (speed * 0.25)
```

All sub-scores are normalized to the range [0, 100]. The composite score is therefore
also in [0, 100].

---

## Quality Score (75% weight)

Weighted acceptance-check pass rate across route benchmarks.

### Route Weights

| Route | Weight | Rationale |
|---|---|---|
| R1 | 0.10 | Simplest route, least config surface. Fast lookup, minimal agent involvement. |
| R2 | 0.25 | Worker model + effort. Tests model selection and effort-level tuning. |
| R3 | 0.40 | Widest config surface: swarm size, lane caps, dispatch strategy, convergence points. Most sensitive to mutation. |
| R4 | 0.25 | Reviewer-heavy, auth/security focus. Tests conservative governance behavior under mutation. |

### Check Types

Quality checks are split into two categories with fixed proportions:

- **Binary checks (70% of quality)**: Pass/fail with no partial credit.
  - pytest suite passes
  - tsc compiles without errors
  - Expected output files exist
- **Continuous checks (30% of quality)**: Graded on a scale.
  - Output structure matches expected schema
  - Word count within acceptable range
  - Completeness of generated artifacts

### Retry Penalty

Each retry incurs a **-10 point penalty** on the quality score, up to a maximum
of **-30** (3 retries). This discourages flaky configurations that only pass
intermittently.

---

## Speed Score (25% weight)

Measures wall-clock execution time relative to the baseline configuration.

```
speed = 100 - ((actual_time / baseline_time) - 0.5) * 66.7
```

### Reference Points

| Scenario | actual / baseline | Speed Score |
|---|---|---|
| 2x faster | 0.50 | 100 |
| No change | 1.00 | ~67 |
| At baseline | 1.00 | 50 (by design: neutral) |
| 2x slower | 2.00 | 0 |

Correction: working through the formula precisely:

| actual / baseline | Calculation | Speed Score |
|---|---|---|
| 0.50 | `100 - (0.50 - 0.50) * 66.7` | 100.0 |
| 0.75 | `100 - (0.75 - 0.50) * 66.7` | 83.3 |
| 1.00 | `100 - (1.00 - 0.50) * 66.7` | 66.7 |
| 1.25 | `100 - (1.25 - 0.50) * 66.7` | 50.0 |
| 1.50 | `100 - (1.50 - 0.50) * 66.7` | 33.3 |
| 2.00 | `100 - (2.00 - 0.50) * 66.7` | 0.0 |

The score is **clamped to [0, 100]**. Configurations faster than 2x baseline still
score 100; configurations slower than 2x baseline still score 0.

---

## Noise Band

**Default: 3 points.**

Composite score improvements at or below the noise band are treated as **inconclusive**
and discarded. This prevents churn from measurement variance, system load fluctuations,
and non-deterministic model behavior.

---

## Confirmation Trial

For improvements between the noise band (3 points) and 15 points, a confirmation
trial is required to distinguish real gains from noise.

### Procedure

1. **Restore baseline config** -- revert to the configuration that produced the current baseline score.
2. **Run benchmark suite** -- fresh baseline measurement under current system conditions.
3. **Apply mutation** -- switch to the candidate configuration.
4. **Run benchmark suite** -- fresh mutation measurement under the same conditions.
5. **Compare paired results** -- if the mutation still wins by more than the noise band, **KEEP**. Otherwise, **DISCARD**.

### Skip Condition

If the initial improvement exceeds **15 points**, skip confirmation entirely. Gains
of this magnitude are clearly real and do not need paired validation.

---

## Benchmark Weights by Phase

Not all benchmarks run in every optimization phase. Early phases use lightweight
benchmarks for fast iteration; later phases add heavier benchmarks for full coverage.

| Phase | Benchmarks Included | Rationale |
|---|---|---|
| 1-2 | R1 + R2 only | Fast turnaround. Validates basic config sanity (model, effort) without waiting for swarm benchmarks. |
| 3 | R3 + R4 only | Topology-relevant. Tests swarm sizing, lane caps, and reviewer governance -- the parameters being actively tuned in this phase. |
| 4-5 | All (R1 + R2 + R3 + R4) | Full regression coverage. Ensures that late-phase mutations do not regress earlier gains. |
