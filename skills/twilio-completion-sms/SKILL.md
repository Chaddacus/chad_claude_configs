---
name: twilio-completion-sms
description: Send SMS notifications through Twilio when Codex completes work. Use when the user asks for text completion alerts, success/failure result notifications, or "text me when done" behavior for long-running tasks.
disable-model-invocation: true
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names,destructive_rollback,branch_policy_live
---

# Twilio Completion SMS

## Overview
Send a final SMS after a task completes by running `scripts/send_sms.py`.
Use dry-run mode before the live send whenever inputs are newly provided.

This skill owns Twilio SMS delivery only. Global and workspace policy own when completion notifications are required and what task state counts as complete.

## Configure Environment
Set these variables before sending:

```bash
export TWILIO_ACCOUNT_SID="ACXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
export TWILIO_AUTH_TOKEN="your_auth_token"
export TWILIO_FROM_NUMBER="+15551234567"
export TWILIO_TO_NUMBER="+15557654321"
```

Use E.164 phone format (`+` plus country code and number).
Do not print or store the auth token in logs, commits, or responses.

## Execute Workflow
1. Finish the requested task first.
2. Build a concise completion message with outcome and key result.
3. Set script path:

```bash
SKILL_DIR="${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}/skills/twilio-completion-sms"
```

4. Run a dry-run check:

```bash
python3 "$SKILL_DIR/scripts/send_sms.py" --status success --task "<task-name>" --details "<short-result>" --dry-run
```

5. Send the live message:

```bash
python3 "$SKILL_DIR/scripts/send_sms.py" --status success --task "<task-name>" --details "<short-result>"
```

6. For explicit custom text, override with `--body`:

```bash
python3 "$SKILL_DIR/scripts/send_sms.py" --body "Task complete: dashboard deployed to production."
```

## Handle Failures
If the SMS send fails, report the exact error and still return the completed task output.
If environment variables or phone formats are invalid, correct inputs before retrying.
Do not retry more than twice without changing inputs or approach.

## Script Reference
Use `scripts/send_sms.py`.

Accepted arguments:
- `--to` and `--from` override `TWILIO_TO_NUMBER` and `TWILIO_FROM_NUMBER`
- `--status` chooses `success` or `failure` for generated text
- `--task` adds a short task label to generated text
- `--details` adds a short summary to generated text
- `--body` sets a fully custom message body
- `--dry-run` validates and prints payload without calling Twilio
