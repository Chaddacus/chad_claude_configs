---
name: test-gaps
description: "Find files with low test coverage that have changed recently. Runs pytest or jest/vitest, cross-references with `git log --since`, and emits a Markdown report ranking 'no test touches it' first, 'coverage below threshold' second."
effort: low
argument-hint: "[--threshold N] [--days N] [--repo PATH] [--no-run] [--out PATH]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
---

# /test-gaps — Recent-Churn × Low-Coverage Cross-Reference

Find the highest-leverage gaps in your test suite: files that have changed in the last N days and either have no test coverage at all or fall below a threshold.

This is **on-demand only**. No cron, no daemon, no background worker. Run it when you want the answer.

## Usage

```bash
/test-gaps                              # cwd, threshold 60%, last 7 days
/test-gaps --threshold 80 --days 14     # tighter bar, longer window
/test-gaps --repo ~/code/some-other-project  <!-- pointer-check:skip -->
/test-gaps --no-run                     # reuse last coverage report (skip pytest/jest run)
```

The skill writes a Markdown report to:
`~/.claude/reports/test-gaps/{YYYY-MM-DD}-{repo-slug}.md`

And prints a one-line summary on stdout.

## Supported Runners (Day 1)

| Runner | Detection | Coverage command |
|---|---|---|
| `pytest` | `pyproject.toml [tool.pytest]` / `pytest.ini` / `conftest.py` / any `test_*.py` | `pytest --cov=. --cov-report=json:.test-gaps-coverage.json` |
| `jest` / `vitest` | `package.json` declares `jest` or `vitest`, or jest/vitest config file | `npx jest --coverage --coverageReporters=json-summary` (or `vitest run --coverage`) |

`cargo` / `go test` are deferred — add a runner branch in `scripts/test_gaps.py` if/when needed.

## Workflow

1. **Detect runner** — `scripts/detect_runner.sh` returns `pytest`, `jest`, or `unsupported`. Bail on `unsupported`.
2. **Run coverage** — `pytest --cov` or `npx jest --coverage`, tolerating test failures (we want the coverage data even if tests fail).
3. **Parse per-file coverage %** — pytest `coverage.json` `files.<path>.summary.percent_covered`, or jest `coverage-summary.json` `<path>.lines.pct`.
4. **Recent files** — `git log --since="<days> days ago" --pretty=format: --name-only`, filtered to source extensions for the detected runner, excluding tests/fixtures/`node_modules`/`__pycache__`/`dist`/etc.
5. **Cross-reference and bucket:**
   - **Priority 1: tracked-no-coverage** — recent file appears nowhere in coverage report (no test imports it). Highest priority because the file has zero test exercise.
   - **Priority 2: below-threshold** — recent file is in the report but coverage < threshold.
   - **Reference: above-threshold** — recent file is in the report and ≥ threshold (shown for sanity, not action).
6. **Render Markdown** — three sections, two tables (P2 + reference). P1 is just a bullet list of paths.
7. **Write report + stdout summary.**

## Defaults

| Flag | Default |
|---|---|
| `--threshold` | `60` (% — flat across languages) |
| `--days` | `7` |
| `--repo` | current working directory |
| `--out` | `~/.claude/reports/test-gaps/{date}-{repo-slug}.md` |

Override via flags. No config file. No publish to chadacus.dev (these are internal repo metrics, not public-facing).

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | Report generated |
| 2 | Repo path invalid |
| 3 | No supported runner detected |
| 4 | Runner ran but produced no coverage report |
| 5 | Coverage report exists but parsed empty |

Treat exit 4 / 5 as "test deps probably aren't installed" — fix that, then re-run with `--no-run` to skip the (failed) re-execution.

## When NOT to use this

- One-off scripts and demos — coverage churn analysis is for repos you're actively maintaining.
- Repos where you've explicitly chosen "no tests" as policy (don't lecture yourself with the report).
- During active TDD on a single feature — recency window will be saturated by the file you're working on. Wait until the feature lands and the window has more signal.

## Implementation files

- `scripts/detect_runner.sh` — repo-signal-based runner detection
- `scripts/test_gaps.py` — orchestrator: run, parse, cross-reference, render
- (no third script — keeping the surface tight)

## Future slices (deferred)

- `cargo tarpaulin` and `go test -coverprofile` runners — add when a Rust/Go repo needs it.
- Daily cron + history dashboard — add when you find yourself running this every morning anyway.
- Auto-stub-generation of missing tests via worker subagent — premature; ship only after `/test-gaps` itself is in regular use.
