---
name: whatsapp-completion
description: Send WhatsApp completion notifications after Codex finishes a task by using WhatsApp Cloud API directly. Use when the user asks for WhatsApp alerts, completion pings, failure notifications, or "message me on WhatsApp when done" behavior.
disable-model-invocation: true
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names,destructive_rollback,branch_policy_live
---

# WhatsApp Completion

## Overview
Send a final WhatsApp message after task completion by running `scripts/send_whatsapp.py`.
Use dry-run mode before live sends whenever numbers, API version, or message format changed.

This skill owns WhatsApp delivery only. Global and workspace policy own when completion notifications are required and what task state counts as complete.

## Configure Environment
Set these variables before sending:

```bash
export META_WHATSAPP_ACCESS_TOKEN="your_whatsapp_cloud_access_token"
export META_WHATSAPP_PHONE_NUMBER_ID="123456789012345"
export META_WHATSAPP_API_VERSION="v21.0"
export WHATSAPP_TO_NUMBER="+15557654321"
```

`WHATSAPP_TO_NUMBER` may be set as either `+1555...`, `1555...`, or `whatsapp:+1555...`.
The script normalizes this to digit-only format expected by WhatsApp Cloud API.
Do not print or store the access token in logs, commits, or responses.

## Execute Workflow
1. Complete the requested task first.
2. Build a concise completion summary.
3. Set script path:

```bash
SKILL_DIR="${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/whatsapp-completion"
```

4. Run a dry-run check:

```bash
python3 "$SKILL_DIR/scripts/send_whatsapp.py" --status success --task "<task-name>" --details "<short-result>" --dry-run
```

5. Send the live message:

```bash
python3 "$SKILL_DIR/scripts/send_whatsapp.py" --status success --task "<task-name>" --details "<short-result>"
```

6. For explicit custom text, override with `--body`:

```bash
python3 "$SKILL_DIR/scripts/send_whatsapp.py" --body "Task complete: migration finished and checks passed."
```

## Handle Failures
If sending fails, report the exact error while still returning task results.
If the API rejects recipient or token, verify the recipient format, phone number ID, and token scopes before retrying.
Do not retry more than twice without changing inputs or configuration.

## Script Reference
Use `scripts/send_whatsapp.py`.

Accepted arguments:
- `--to` overrides `WHATSAPP_TO_NUMBER`
- `--phone-number-id` overrides `META_WHATSAPP_PHONE_NUMBER_ID`
- `--api-version` overrides `META_WHATSAPP_API_VERSION`
- `--status` chooses `success` or `failure` for generated text
- `--task` adds a short task label
- `--details` adds a short summary
- `--body` sets a fully custom message
- `--dry-run` validates and prints payload without calling the API
