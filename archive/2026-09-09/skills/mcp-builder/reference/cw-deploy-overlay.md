# CW Deployment Overlay (Chad-internal)

Local extension to mcp-builder. Load **only** when the MCP you're scaffolding is destined for a CloudWarriors / kickstarter shared host (noob-root, customer noob-boxes) — not for local-only dev tools that live under `~/.claude.json`.

The base mcp-builder skill is upstream-vanilla. This overlay captures the CW-specific layers that the official guide cannot know about: kickstarter compose pinning, Tailscale Serve routing, identity gating, and the GHCR tag-bump deploy flow.

> ⚠️ Patterns here are snapshots from omni-mem (verified 2026-04-22 era) and may drift. Before relying on a specific path, file, or env var, verify against the live repo at `~/code/omni-mem/` and the live tailnet mounts via `tailscale serve status`.

---

## When this overlay applies

| Scenario | Apply overlay? |
|----------|----------------|
| Local tool MCP for Claude Code (per-user, registered in `~/.claude.json`) | No — vanilla mcp-builder is complete. |
| MCP that ships in a kickstarter rollout (`ks-*` namespace) | **Yes.** |
| MCP that runs on noob-root or any shared tailnet host | **Yes.** |
| MCP that gates on tailnet identity (`tailscale-user-login`) | **Yes.** |
| One-off scratch MCP under `~/code/<thing>-mcp/` | No, unless it later graduates to shared deploy. |

If unsure: scaffold vanilla first, retrofit the overlay later. It's additive.

---

## Transport choice — diverges from upstream default

Upstream mcp-builder recommends **streamable HTTP + stateless JSON** for remote servers. The current CW pattern in `omni-mem/packages/management-mcp/src/server.ts` uses **stdio with content-length or newline framing** (LSP-style). Both work; pick by deployment model:

- **stdio** if the MCP is launched by a parent process (Claude Code, sidecar, sync-daemon) and the lifecycle is parent-managed. This is what every existing `ks-*` MCP uses.
- **streamable HTTP** if the MCP is a long-lived service behind Tailscale Serve and needs to multiplex multiple callers. Newer pattern; consider for v2 of any MCP that outgrows single-parent stdio.

Do not switch a working stdio MCP to HTTP without a specific scaling reason. The kickstarter deploy story is built around the stdio shape today.

---

## Deployment surface — kickstarter compose

CW shared MCPs deploy through `cw-ai-kickstarter`, **not** the MCP repo's own compose file. The MCP repo's `docker-compose.yml` is for teammate workstation install via `scripts/install-*-node.sh`; the noob-root hosts run a different compose project.

```
/root/web/cw-ai-kickstarter/deploy/docker-compose.kickstarter.yml
  service: kickstarter-<mcp-name>
  image:   ghcr.io/cloudwarriors-ai/<mcp-name>:sha-<7char>   ← pinned SHA, not :latest
  ports:   127.0.0.1:${<NAME>_PORT:-<n>}:<n>                  ← loopback only
```

**Why loopback-only:** defense-in-depth so the container is only reachable through the tailscale-serve HTTPS proxy. Even if the host firewall changes, the container has no public attack surface.

---

## GHCR tag-bump deploy flow (post-merge → live)

1. Merge PR. Wait for `.github/workflows/publish-image.yml` to push `sha-<7char>` to GHCR (5–15 min):
   ```bash
   gh run list --workflow publish-image
   ```
2. SSH and bump the pinned tag:
   ```bash
   ssh noob-root
   cd /root/web/cw-ai-kickstarter/deploy
   cp docker-compose.kickstarter.yml \
      docker-compose.kickstarter.yml.bak-$(date +%Y%m%d-%H%M%S)-<reason>
   sed -i 's|ghcr.io/cloudwarriors-ai/<mcp>:<old-tag>|ghcr.io/cloudwarriors-ai/<mcp>:sha-<new>|' \
      docker-compose.kickstarter.yml
   docker compose -f docker-compose.kickstarter.yml pull kickstarter-<mcp>
   docker compose -f docker-compose.kickstarter.yml up -d kickstarter-<mcp>
   curl -fsS http://127.0.0.1:<port>/health
   ```
3. Verify over the tailnet URL too: `curl https://noob-root.tailcc6c5f.ts.net/<mount>/health`.

**Never** assume `docker compose build` from the repo updates noob-root — that only updates dev workstations. **Never** rely on `:latest` — pinned SHA tags are the contract.

---

## Tailscale Serve mounts — public URL shape

Each MCP gets a `/path` mount on the shared `https://noob-root.tailcc6c5f.ts.net` surface:

```
https://noob-root.tailcc6c5f.ts.net
├── /omni      → 127.0.0.1:8765
├── /dashboard → 127.0.0.1:8765/dashboard
├── /mesh      → 127.0.0.1:18791
└── /<your-mcp> → 127.0.0.1:<your-port>
```

Add a mount (additive, doesn't disturb existing):
```bash
tailscale serve --bg --https=443 --set-path=/<mount> http://127.0.0.1:<port>/<path>
tailscale serve status   # confirm
```

**Strip-prefix behavior:** `/omni/X` arrives at the container as `/X`. If your MCP serves assets with absolute paths (e.g. a dashboard SPA with a Vite base), you have two options:
- (a) build the asset paths to match the mount prefix (`base: '/myapi/'` in Vite)
- (b) mount with the prefix preserved by appending it to the backend URL

---

## Identity gating — tailnet user headers

If your MCP exposes endpoints that act on behalf of a user, gate them via the tailscale-serve-added headers. Reference implementation: `omni-mem/packages/cli/src/index.ts:verifyTailscaleUser`.

Required headers (added automatically by tailscale-serve to any request that traverses it):
- `tailscale-user-login` (the email)
- All three `X-Forwarded-*` headers

Verification order:
1. `<MCP>_TRUST_USER_HEADER=1` → trust raw `tailscale-user-login`. **DEV-ONLY.** Never set on noob-root.
2. Real request: has all three `X-Forwarded-*` headers AND (came from loopback OR `<MCP>_TRUST_PROXY=1`).

**Probe gotcha:** `docker exec ... curl -H "tailscale-user-login: chad@..." http://127.0.0.1:<port>/api/...` fails with `tailscale_user_required` even though it's loopback — because the `X-Forwarded-*` headers only exist when tailscale-serve adds them. Probe from a tailnet machine, not from inside the container.

---

## Build/image conventions

Match the existing pattern:

- **Dockerfile name:** `Dockerfile.debian` at repo root (multi-stage; node:20-slim base for TS MCPs).
- **GHCR repo:** `ghcr.io/cloudwarriors-ai/<mcp-name>`.
- **Tag scheme:** `sha-<7char>` (from `${{ github.sha }}`), plus optional semver tags `vX.Y.Z-<flavor>` for branch artifacts.
- **Publish workflow:** `.github/workflows/publish-image.yml`. Trigger on `push` to `main` + `workflow_dispatch`.

Copy the omni-mem workflow as the template; the only repo-specific bits are the image name and the build context.

---

## Registration in Claude Code

After deploy, register the MCP in user/managed settings so Claude Code clients can discover it.

**Per-user (Chad):** `~/.claude.json` under `mcpServers`. Already populated with `ks-feedback-mcp`, `ks-omni-mem`, `ks-rapture-gateway`, `ks-sentinel`, `ks-cloudwarriors-ai-hash-svc`, `ks-cloudwarriors-ai-qr-svc`, `ks-currenttime`, `ks-deployment-mcp`, `ks-sentinel-rest`, `ks-smoke-mcp`. Pattern:
```json
{
  "mcpServers": {
    "ks-<name>": {
      "command": "...",   // for stdio (rare for shared deploys)
      "url": "https://noob-root.tailcc6c5f.ts.net/<mount>"   // for HTTP
    }
  }
}
```

**Managed (org-wide via MDM):** the kickstarter rollout writes `managed-mcp.json` to:
- macOS: `/Library/Application Support/ClaudeCode/managed-mcp.json`
- Linux/WSL: `/etc/claude-code/managed-mcp.json`
- Windows: `C:\Program Files\ClaudeCode\managed-mcp.json`

Drop-in directory `managed-mcp.d/` is supported (v2.1.83+) for independent policy fragments.

---

## What stays vanilla

Everything in the upstream skill — tool naming, error messages, output schemas, eval framework, language guides — applies unchanged. This overlay only adds the deployment layer.

In particular: **always create a 10-question evaluation XML** (per `reference/evaluation.md` and `scripts/evaluation.py`). The CW MCP fleet has zero standardized evals today; new MCPs should not perpetuate that gap.
