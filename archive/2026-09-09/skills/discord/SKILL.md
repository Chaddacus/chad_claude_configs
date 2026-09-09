---
name: discord
description: "Manage Chad's Discord servers via REST API (bot token from Bitwarden) — channels, roles, permissions, topics, posting. Use for setup, restructure, audit, or posting to Discord."
---

# Discord management

A bot-token + REST capability for managing Chad's Discord servers. No running service, no MCP — the token lives in Bitwarden and is pulled per-call via `rbw`.

## Auth model

- The bot token is the password field of a Bitwarden item; read it with `rbw get "<item>"` and **never print it**. See the global `## Secret Access (Bitwarden via rbw)` note. If `rbw` reports the agent locked, stop and ask Chad to `rbw unlock`.
- The CLI does this for you: `--server <name>` resolves `{guild, token_item}` from `~/.config/chadacys_discord/servers.json`, then calls `rbw get` internally.
- **Gotcha (load-bearing):** Discord's edge requires a real `User-Agent` header. urllib's default UA is blocked by Cloudflare with `error code: 1010` before Discord sees the request. The CLI always sends one; any hand-rolled `curl`/script must too.

## The CLI

`~/.claude/skills/discord/discord.py` — one tool, subcommands. Resolve a target with `--server chadacys` (or `--guild <id> --token-item "<item>"`).

```bash
D=~/.claude/skills/discord/discord.py
python3 $D inventory --server chadacys                 # channels + roles + ids (read-only; start here)
python3 $D me        --server chadacys                 # bot identity
python3 $D guilds    --server chadacys                 # servers the bot is in
python3 $D category  --server chadacys --name "NEW CAT" [--private]
python3 $D channel   --server chadacys --name new-chan --parent <cat_id> --type text|voice|announce|forum
python3 $D role      --server chadacys --name Supporter --color 14917658 --hoist --mentionable
python3 $D delete-role --server chadacys --role Scout    # by name or id — DESTRUCTIVE, confirm first
python3 $D topic     --server chadacys --channel welcome --text "Start here."
python3 $D perms     --server chadacys --channel tw-beta --role Playtester --allow 1024   # VIEW_CHANNEL=1024
python3 $D post      --server chadacys --channel announcements --content "..." [--pin]
python3 $D rename-server --server chadacys --name Chadacys
python3 $D invite    --server chadacys [--channel welcome]   # never-expiring, unlimited-use join link
```

Channels and roles can be referenced by **name or id** (`topic`, `perms`, `delete-role`, `post`). Permission values are raw bitfields (VIEW_CHANNEL = 1024). To make a channel private: `perms --role @everyone-id --deny 1024` then `perms --role <role> --allow 1024` (or `category --private`, which denies VIEW to @everyone at creation).

To register another server, add an entry to `~/.config/chadacys_discord/servers.json`.

## Safety rules

- **Posting messages is an EXTERNAL action.** Draft the copy, show it to Chad, and post only after explicit approval. Expect the auto-mode classifier to gate live `post` calls even with approval — when it does, hand Chad paste-ready copy blocks instead of fighting the gate. Structural ops (category/channel/role/topic/perms) are config, not external comms, and run fine.
- **Deletes are destructive.** `delete-role` strips the role from every member who has it; deleting a channel loses its history. Confirm with Chad before any delete.
- **Idempotency is on you.** The CLI does not dedupe — check `inventory` before creating, or you'll get duplicates. (The original one-off build scripts in `~/.config/chadacys_discord/tw_discord_*.py` were idempotent by name; the CLI is not.)
- **Never print the token.** `rbw get` output is a secret; interpolate, never echo.

## Known servers

- **chadacys** — guild `1444869387172712622`, bot `quartermaster`, token item `"Treasure Wake Discord Bot"`. Structure: INFO / GAMES-divider / TREASURE WAKE / BOOKS-divider / SPARK OF DEFIANCE / COMMUNITY / STAFF(private). Roles: `Playtester` (gold, hoisted) for the Treasure Wake closed beta — **manually granted only, not self-serve** (it unlocks the beta channels). `Reader` for Spark of Defiance. Self-serve interest roles `Announcements` + `Treasure Wake` (+ existing `Reader`) are assigned via **native Discord Onboarding** — `PUT /guilds/{id}/onboarding`, a "Pick your interests" prompt (see `~/.config/chadacys_discord/tw_onboarding.py`). No bot listener needed: Discord hosts the picker (members reach it via the server name → "Channels & Roles", and new members in the join flow). quartermaster is REST-only so it can't run a reaction/button listener — onboarding is the no-service way to do self-serve roles. **Onboarding gotchas:** every `default_channel_ids` entry must grant @everyone VIEW (a stale @everyone view-deny inherited on `#general` from its old `chill-zone` identity blocked enable with `DEFAULT_CHANNEL_REQUIRES_EVERYONE_ACCESS` until cleared); a send-denied channel can't be a default channel either. (The short-lived carl-bot `#roles` panel channel was deleted once native onboarding replaced it.) Beta channels (`tw-beta`/`tw-bugs`/`tw-feedback`) are Playtester-gated.
