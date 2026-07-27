---
name: notify
description: Send completion notifications through SMS, WhatsApp, or desktop. Use when the user asks for completion alerts, "text me when done", or "notify me" behavior for long-running tasks.
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names,destructive_rollback,branch_policy_live
---

# Notify

## Overview
Send a completion notification after a task finishes by running `~/.claude/bin/notify_done.sh`.
This skill wraps the unified notification dispatcher which supports SMS (Twilio), WhatsApp, and desktop channels.

This skill owns notification delivery only. Global and workspace policy own when completion notifications are required and what task state counts as complete.

## Channel Configuration

### Desktop (always available on macOS)
No configuration needed. Uses `osascript` or `terminal-notifier`.

### SMS (Twilio)
Set variables in `~/.config/codex/secrets/twilio.env`:
```bash
TWILIO_ACCOUNT_SID="ACXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
TWILIO_AUTH_TOKEN="your_auth_token"
TWILIO_FROM_NUMBER="+15551234567"
TWILIO_TO_NUMBER="+15557654321"
```

### WhatsApp
Set variables in `~/.config/codex/secrets/whatsapp.env`:  <!-- pointer-check:skip -->
```bash
META_WHATSAPP_ACCESS_TOKEN="your_whatsapp_cloud_access_token"
META_WHATSAPP_PHONE_NUMBER_ID="123456789012345"
META_WHATSAPP_API_VERSION="v21.0"
WHATSAPP_TO_NUMBER="+15557654321"
```

Do not print or store auth tokens in logs, commits, or responses.

## Execute Workflow
1. Finish the requested task first.
2. Build a concise completion message with outcome and key result.
3. Send notification:

```bash
# Auto-select channel (tries sms, whatsapp, desktop in order)
~/.claude/bin/notify_done.sh --status success --task "<task-name>" --details "<short-result>"

# Explicit channel selection
~/.claude/bin/notify_done.sh --status success --task "<task-name>" --details "<short-result>" --channel sms
~/.claude/bin/notify_done.sh --status success --task "<task-name>" --details "<short-result>" --channel whatsapp
~/.claude/bin/notify_done.sh --status success --task "<task-name>" --details "<short-result>" --channel desktop

# Custom message body
~/.claude/bin/notify_done.sh --body "Task complete: dashboard deployed to production." --channel sms
```

4. For dry-run validation:
```bash
~/.claude/bin/notify_done.sh --status success --task "<task-name>" --details "<short-result>" --dry-run
```

## Handle Failures
If the notification send fails, report the exact error and still return the completed task output.
If environment variables or phone formats are invalid, correct inputs before retrying.
Do not retry more than twice without changing inputs or approach.

## Script Reference
Use `~/.claude/bin/notify_done.sh`.

Accepted arguments:
- `--status <success|failure>` — completion status (default: success)
- `--task <text>` — short task name
- `--details <text>` — short completion detail
- `--body <text>` — explicit message body (overrides generated text)
- `--channel <auto|sms|whatsapp|desktop>` — channel selection (default: auto)
- `--env-file <path>` — env file override
- `--started-at <epoch-seconds>` — start time for elapsed duration
- `--urgent` — bypass quiet-hours suppression
- `--force` — bypass duplicate suppression
- `--dry-run` — validate and print payload without sending
