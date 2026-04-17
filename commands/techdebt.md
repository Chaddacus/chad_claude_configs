---
description: End-of-session scan for duplication, dead code, stale TODOs, and high-confidence cleanups. Report only — do not edit.
---

# /techdebt

Scan the current repository for high-confidence cleanup targets. Report as a ranked list. **Do not apply fixes** — a separate invocation does that with user approval.

## Scans

1. **Duplicated code blocks** — `rg --no-heading -n "." | awk '{print length, $0}' | sort -rn` is too naive; use structural similarity: `jscpd --min-lines 6 --min-tokens 50` if available, else grep-pair identical ≥6-line spans.
2. **Unused imports / exports** — Python: `ruff check --select F401,F841`. TS/JS: `npx knip` if available, else `tsc --noEmit --listFiles | xargs …`.
3. **Stale TODO/FIXME/XXX** — `rg -n "TODO|FIXME|XXX"` then for each, `git log -S "<text>" --format="%ad" | head -1` to date it; flag > 90 days old.
4. **Dead conditional branches** — constants that never change, `if false` blocks, always-truthy guards.
5. **Commented-out code** — blocks of 3+ consecutive comment-only lines that parse as code.
6. **Obvious inefficiencies** — N+1 loops near DB calls, sync FS in async handlers, `.map(...).filter(...)` that could be a single pass.

## Report format

```
## Priority (impact × ease / risk)

### P1 (ship now)
- <path:line> — <one-line rationale>

### P2 (batch next cleanup round)
### P3 (defer; note why)
```

## Hard rules
- No edits.
- No findings below confidence 0.8 — false positives burn trust.
- If a "smell" is actually a deliberate pattern explained in CLAUDE.md or a comment, exclude it.
