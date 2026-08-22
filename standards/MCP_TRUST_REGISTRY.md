# MCP Trust Registry (Standard 8)

Machine-level trust classification for every configured MCP server, per
`~/.claude/rules/security-secrets.md`: **T0** local/read-only · **T1** trusted external read-only ·
**T2** trusted bounded write · **T3** protected production/admin — unknown/unreviewed is prohibited
until reviewed. This file is data, not policy; reclassifying a server is a config edit through the
normal PR flow.

First pass classified 2026-08-22 from the audit in that day's session. Classifications marked
`(first-pass)` await Chad's ratification; the two marked **UNREVIEWED** and the pending approvals
are his acts, not the agent's.

## Active servers

| Server | Class | Basis / notes |
|---|---|---|
| cw-standards | T0 | Local read-only standards server (`~/.cw-ai-configs`) |
| plugin:second-opinion:codex | T0 | Local codex consult, no external writes |
| stitch | T0 | Local design server. **DEGRADED** — tools fetch fails on `#/$defs/ScreenInstance`; upstream schema fix needed, tracked here |
| claude_design | T1 | Anthropic-hosted design generation; no writes to our systems |
| claude.ai Google Drive | T2 (first-pass) | Anthropic-managed connector; bounded writes to Chad's own Drive |
| claude.ai Google Calendar | T2 (first-pass) | Same; calendar writes are user-visible and reversible |
| claude.ai Gmail | T2 (first-pass) | Same, but **sends are irreversible external actions** — confirm recipient before send |
| chad-agent | T2 | Zoom posts/DMs/meetings **as Chad** — public irreversible sends; destination confirmed first (server's own instruction) |
| chad-away-responder | T2 (first-pass) | Local service, bounded responder writes |
| chad-marketing | T2 (first-pass) | Local service, bounded writes |
| sentinel | T2 | Local test-generation/metrics writes (work plane). Scope conflict resolved 2026-08-22: project scope (`~/chad_work/.mcp.json`) is canonical; user-scope duplicate removed |
| omni-mem-manage | T2 | Local memory administration (work vault) |
| devrelay | T3 (first-pass) | Fleet dispatch, review-gate, release actions — release authority governed by its own coordinator playbook + S2 clear-to-merge doctrine |
| rapture-bypass-tailscale | **T3** | Staff-only platform administration over client tenants (Zoom Phone/Dialpad/RC). Behind the `ask` permission gate — restored 2026-08-22 (PR #15) after a server rename had silently unhooked it |
| dev-mcp-gateway | **UNREVIEWED** | External proxy (`mcp-gateway.pscx.ai`) — the effective tool surface is whatever sits behind the proxy. Owner skipped the review 2026-08-22; status accepted. Remains UNREVIEWED: not for consequential work per Standard 8 |

## Approved by owner 2026-08-22 (per-name via settings `enabledMcpjsonServers` — no blanket trust)

Chad approved all pending servers on 2026-08-22 ("you dont need me — 1 approve, 2 approve").
Recorded as named entries in `settings.json` `enabledMcpjsonServers`, not `enableAllProjectMcpServers`,
so a future repository's `.mcp.json` still prompts. All verified ✔ Connected after approval.

| Server | Class | Notes |
|---|---|---|
| sentinel | T2 | Approval carried to the surviving project-scope definition |
| omni-mem | T2 | Local memory MCP. `mcp__omni-mem__*` allow-list entries restored on approval, per the documented re-add path |
| playwright | T2 | Local browser automation (MCP form; `playwright` skill remains the terminal-driven form) |
| wigolo | T2, secrets-adjacent | Wraps 1Password (`wigolo-op.sh`) — owner approved with the secrets note on record |
| cloudwarriors | T1 | `@cloudwarriors-ai/mcp-docs`, read-only docs |
| openaiDeveloperDocs | T1 | **Repaired 2026-08-22**: the configured npm package (`@openai/mcp-server-openai-developer-docs`) never existed on the registry (404 — dead since it was added). Replaced with OpenAI's hosted endpoint `https://developers.openai.com/mcp` in `~/.mcp.json`; connects |
| claude-code-docs | T1 | **Added 2026-08-22** at user scope: Anthropic's hosted Claude Code docs server, `https://code.claude.com/docs/mcp` (full-text docs search, no auth) |

## Related decisions of record (2026-08-22 audit)

- **Unpinned main agents are deliberate** for `chad-twin`, `chad-work`, `chad-personal` (tree-bound
  main-session agents inherit the session model). `chad-agent` is pinned `sonnet` — it is routinely
  *spawned* for comms tasks and an unpinned spawn drew a live R2 route-gate denial on 2026-08-20.
- **Skills retired to `~/.claude/skills-archive/`** (archive, never delete): `zw-lead-b-status`
  (single-sprint), `rebecca-monitor` (single-project debug), `companion` (gimmick),
  `webapp-testing` (superseded — `playwright` skill + foundation `ui-proof` are the two browser
  surfaces, one for driving, one for proof).
- **Legacy protocol cluster untouched** (`build`, `drive`, `audit`, `analyze`, `autoconfig`) —
  flagged as second-policy risk in the audit; retire-or-reconcile is Chad's call (uphill).
