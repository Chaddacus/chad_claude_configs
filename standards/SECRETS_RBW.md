---
policy_doc_kind: secrets_runbook
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names
---

# Secret Access (Bitwarden via rbw) — usage runbook

Extracted from `~/.claude/CLAUDE.md` on 2026-07-05 (conciseness pass: the constitution
keeps the normative rules; this doc owns the usage detail).

Chad's secrets live in his Bitwarden vault, reachable from any shell via `rbw` (installed + configured 2026-06-21: account `chad3124@gmail.com`, official cloud, 30-day unlock timeout, `pinentry-mac` GUI prompt). This is the canonical way to obtain credentials — never ask Chad to paste a secret into chat or write it to a plaintext file.

- Read a secret: `rbw get "<item name>"` (returns the item's password field, e.g. tokens/keys); `rbw get --full "<item name>"` for notes; `rbw list` enumerates item names.
- NEVER print a secret value. Interpolate inline, e.g. `curl -H "Authorization: Bot $(rbw get 'Treasure Wake Discord Bot')"`.
- If `rbw get` fails with the agent locked / "agent not running" (e.g. after a reboot), STOP and ask Chad to run `rbw unlock` once — unlocking needs his master password via the GUI prompt and cannot be automated from a non-interactive shell. Login/unlock are Chad's interactive acts; reading unlocked secrets is yours.
