#!/usr/bin/env bash
# thoughts_md_readiness.sh — Phase A prerequisite probe.
#
# Verifies the environment for the thoughts.md autonomy roadmap (slices 0-8).
# Exits 0 when healthy, non-zero with a fix-it message otherwise.
#
# Plan: ~/.claude/plans/users-chadsimon-thoughts-md-take-a-giggly-moore.md (Slice 0)

set -uo pipefail

readonly OK="\033[32m✓\033[0m"
readonly FAIL="\033[31m✗\033[0m"
readonly WARN="\033[33m!\033[0m"

failures=0
warnings=0

ok()   { printf "  ${OK} %s\n" "$1"; }
fail() { printf "  ${FAIL} %s\n" "$1"; failures=$((failures+1)); }
warn() { printf "  ${WARN} %s\n" "$1"; warnings=$((warnings+1)); }
hdr()  { printf "\n\033[1m== %s ==\033[0m\n" "$1"; }

# ----------------------------------------------------------------------------
hdr "core tools"
# ----------------------------------------------------------------------------

for cmd in docker git node npm python3 jq curl; do
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd: $(command -v "$cmd")"
  else
    fail "$cmd: not found in PATH"
  fi
done

# Node version >= 20
if command -v node >/dev/null 2>&1; then
  node_major=$(node -e 'console.log(process.versions.node.split(".")[0])' 2>/dev/null || echo 0)
  if [ "$node_major" -ge 20 ]; then
    ok "node >= 20 (got $(node -v))"
  else
    fail "node version too old (got $(node -v 2>/dev/null || echo unknown), need >= 20)"
  fi
fi

# Python version >= 3.9 (rubric scorers use stdlib only — pathlib, concurrent.futures, asyncio)
if command -v python3 >/dev/null 2>&1; then
  py_minor=$(python3 -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo 0)
  py_major=$(python3 -c 'import sys; print(sys.version_info[0])' 2>/dev/null || echo 0)
  if [ "$py_major" -eq 3 ] && [ "$py_minor" -ge 9 ]; then
    ok "python >= 3.9 (got $(python3 -V 2>&1))"
  else
    fail "python too old (got $(python3 -V 2>&1), need >= 3.9)"
  fi
fi

# ----------------------------------------------------------------------------
hdr "openshield CLI (slice 3 dependency)"
# ----------------------------------------------------------------------------

OPENSHIELD_HOME="${OPENSHIELD_HOME:-$HOME/code/openshield}"
if [ -d "$OPENSHIELD_HOME" ]; then
  ok "openshield repo at $OPENSHIELD_HOME"
  # Canonical invocation is `npx tsx packages/cli/src/index.ts` (no build step).
  if [ -f "$OPENSHIELD_HOME/packages/cli/src/index.ts" ]; then
    ok "openshield CLI source present (run via npx tsx)"
  else
    fail "openshield CLI sources missing — repo may be corrupt or partial clone"
  fi
  if [ -d "$OPENSHIELD_HOME/node_modules" ]; then
    ok "openshield node_modules present"
  else
    warn "openshield node_modules missing — run: (cd $OPENSHIELD_HOME && npm install)"
  fi
else
  fail "openshield repo missing at $OPENSHIELD_HOME — clone via: git clone https://github.com/cloudwarriors-ai/openshield $OPENSHIELD_HOME"
fi

# ----------------------------------------------------------------------------
hdr "cw-ai-configs (slice 3 — security-audit skill source)"
# ----------------------------------------------------------------------------

CWCONFIGS_HOME="${CWCONFIGS_HOME:-$HOME/code/cw-ai-configs}"
if [ -d "$CWCONFIGS_HOME" ]; then
  ok "cw-ai-configs repo at $CWCONFIGS_HOME"
  if [ -f "$CWCONFIGS_HOME/personal/chad/skills/security-audit/SKILL.md" ]; then
    ok "security-audit SKILL present in cw-ai-configs"
  else
    fail "security-audit SKILL missing at $CWCONFIGS_HOME/personal/chad/skills/security-audit/SKILL.md"
  fi
else
  fail "cw-ai-configs missing at $CWCONFIGS_HOME — clone via: git clone https://github.com/cloudwarriors-ai/cw-ai-configs $CWCONFIGS_HOME"
fi

# ----------------------------------------------------------------------------
hdr "sentinel container (slice 2)"
# ----------------------------------------------------------------------------

SENTINEL_HOME="${SENTINEL_HOME:-$HOME/code/sentinel}"
if [ -d "$SENTINEL_HOME" ]; then
  ok "sentinel repo at $SENTINEL_HOME"
else
  warn "sentinel repo missing at $SENTINEL_HOME — clone via: git clone https://github.com/cloudwarriors-ai/sentinel $SENTINEL_HOME"
fi

# Probe SSE endpoint. /sse keeps the connection open by design (Server-Sent
# Events), so a normal `curl -fsS` always exits 28 (timeout). What we actually
# want to verify: did sentinel respond with HTTP 200 at all? curl exits 28
# AFTER receiving headers, and writes %{http_code} either way.
sentinel_status=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://localhost:8100/sse" 2>/dev/null || true)
if [ "$sentinel_status" = "200" ]; then
  ok "sentinel SSE responding on :8100 (HTTP 200)"
else
  warn "sentinel SSE not responding on :8100 (got '$sentinel_status') — bring up via: (cd $SENTINEL_HOME && docker compose up -d)"
fi

# ----------------------------------------------------------------------------
hdr "omni-mem"
# ----------------------------------------------------------------------------

if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^omni-mem$'; then
  ok "omni-mem container running"
  # No `omni-mem readiness` command exists. Use a real, side-effect-free call —
  # `list_domains` returns JSON quickly and proves the CLI + DB are alive.
  if docker exec omni-mem omni-mem list_domains --workspaceId chadsimon >/dev/null 2>&1; then
    ok "omni-mem CLI responding (list_domains ok)"
  else
    warn "omni-mem container running but CLI not responding (tried: list_domains)"
  fi
else
  fail "omni-mem container not running — start via: docker start omni-mem"
fi

# ----------------------------------------------------------------------------
hdr "language tooling (cross-cutting)"
# ----------------------------------------------------------------------------

# JS/TS testing + analysis tooling — global install acceptable
for npmpkg in dependency-cruiser @lhci/cli; do
  if npm ls -g "$npmpkg" --depth=0 >/dev/null 2>&1; then
    ok "$npmpkg installed globally"
  else
    warn "$npmpkg not installed globally — install via: npm i -g $npmpkg"
  fi
done

# Per-project node tooling (axe-core/playwright, fast-check) is checked in slice 8 onboarding,
# not here. Same for Python venv tooling.

# ----------------------------------------------------------------------------
hdr "Python tooling (slice 1 data-combo + slice 3 design scorer)"
# ----------------------------------------------------------------------------

# These check for installation in the *active* python3 environment.
# Slice 1's data-combo flow is per-project; we just verify they're reachable.
for pypkg in schemathesis hypothesis grimp; do
  if python3 -c "import $pypkg" 2>/dev/null; then
    ok "python: $pypkg importable"
  else
    warn "python: $pypkg not in active env — install via: pip install $pypkg"
  fi
done

# ----------------------------------------------------------------------------
hdr "hermes (slice 4 — optional)"
# ----------------------------------------------------------------------------

HERMES_HOME="${HERMES_HOME:-$HOME/code/hermes}"
if [ -d "$HERMES_HOME" ]; then
  ok "hermes repo at $HERMES_HOME (slice 4 will use this)"
else
  warn "hermes repo missing at $HERMES_HOME (slice 4 — Phase B; not required for Phase A)"
fi

# ----------------------------------------------------------------------------
hdr "summary"
# ----------------------------------------------------------------------------

if [ "$failures" -eq 0 ] && [ "$warnings" -eq 0 ]; then
  printf "\nAll checks passed. Ready for Phase A.\n"
  exit 0
elif [ "$failures" -eq 0 ]; then
  printf "\n%d warning(s), 0 failures. Phase A can start; resolve warnings before Phase B/C.\n" "$warnings"
  exit 0
else
  printf "\n%d failure(s), %d warning(s). Resolve failures before continuing.\n" "$failures" "$warnings"
  exit 1
fi
