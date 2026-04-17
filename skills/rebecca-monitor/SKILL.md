---
name: rebecca-monitor
description: Real-time monitoring and debugging for Rebecca/Bighead meeting pipeline
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
---

# /rebecca-monitor

Real-time diagnostic aggregator for the Rebecca + Bighead meeting pipeline. Pulls data from all subsystems (Flask logs, SQLite, Recall.ai API, ngrok, Bighead) into a single structured view for rapid debugging.

## Usage

```
/rebecca-monitor                    # Full diagnostic snapshot
/rebecca-monitor --watch            # Continuous monitoring (re-run every 10s)
/rebecca-monitor --meeting <id>     # Deep-dive on specific meeting
/rebecca-monitor --errors           # Errors and warnings only
/rebecca-monitor --transcript <id>  # Full transcript for a meeting
/rebecca-monitor --webhooks         # Recent ngrok webhook traffic
```

## Flags

| Flag | Effect |
|------|--------|
| `--watch` | Re-run monitor every 10 seconds, diff output between runs |
| `--meeting <id>` | Focus on a single meeting: full state, transcript, bot status, voice events |
| `--errors` | Filter to ERROR/WARNING log lines + failed webhooks + error states |
| `--transcript <id>` | Dump full transcript segments for a meeting with speaker/timing |
| `--webhooks` | Show last 20 ngrok-captured requests with status codes and bodies |
| `--recall` | Show all Recall.ai bot statuses via API |
| `--voice` | Show voice pipeline state: wake word detections, TTS calls, debounce |
| `--json` | Machine-readable JSON output instead of formatted text |

## Workflow

### 1. Run the monitor script

```bash
cd ~/chad_bot_attempt/cw-nemo/cw/code/rebecca_2.0
source .venv/bin/activate && python scripts/monitor.py [flags]
```

The script aggregates from 6 data sources:

| Source | What it provides |
|--------|-----------------|
| `logs/qa_bot.log` | Request logs, meeting events, voice pipeline, errors |
| `data/state.db` | Meeting records, transcript segments, bot mappings |
| Recall.ai API | Live bot status, transcript, recording URLs |
| ngrok API (localhost:4040) | Inbound webhook requests from Recall.ai |
| Bighead API | Health status, transcript analysis availability |
| Process list | Flask/ngrok/tunnel process health |

### 2. Interpret the output

The monitor outputs sections:

**SERVICES** — health of each component (Rebecca, Bighead, ngrok, SSH tunnel)
**MEETINGS** — active and recent meetings with state, bot_id, timestamps
**BOT STATUS** — Recall.ai bot lifecycle state (joining → waiting room → recording → done)
**TRANSCRIPTION** — segment count, last segment time, speakers detected, word count
**WEBHOOKS** — recent inbound requests: path, status code, timestamp, body preview
**VOICE** — wake word detections, TTS invocations, response latency
**ERRORS** — aggregated errors from all sources in chronological order
**ID MAPPINGS** — meeting_id ↔ bot_id ↔ audio_bot_id ↔ livekit_room cross-references

### 3. Debug common issues

| Symptom | Check | Likely cause |
|---------|-------|-------------|
| Bot not joining | BOT STATUS shows READY or ERROR | Meeting URL invalid, Recall.ai quota, or meeting hasn't started |
| Bot stuck in waiting room | BOT STATUS shows IN_WAITING_ROOM | Host hasn't admitted bot; check Zoom meeting settings |
| No transcription arriving | WEBHOOKS section empty | ngrok not running, webhook URL wrong, or signature verification failing |
| Webhook 401s | ERRORS section | RECALL_TRANSCRIPTION_SECRET mismatch between .env and Recall.ai config |
| Voice not responding | VOICE section shows no wake words | Wake word not detected, or debounce blocking (1s cooldown) |
| Transcript analysis failing | BIGHEAD section unhealthy | SSH tunnel down, or Bighead container crashed on inference_box |
| Meeting shows "abandoned" | MEETINGS status=abandoned | Bot was in joining/waiting >30 min, auto-cleaned by cleanup_abandoned() |

### 4. Deep-dive workflow

When a specific problem is found:

1. Run `--meeting <id>` for full meeting context
2. Run `--webhooks` to see if Recall.ai is sending data
3. Run `--transcript <id>` to see what was captured
4. Check `--errors` for any stack traces
5. If Bighead-related: `curl http://localhost:8000/health` and check SSH tunnel
6. If Recall-related: `--recall` to see bot status directly from Recall.ai API

## Key file paths

| File | Purpose |
|------|---------|
| `scripts/monitor.py` | The monitoring script |
| `logs/qa_bot.log` | Flask application log |
| `data/state.db` | SQLite database with meetings + transcripts |
| `.env` | Environment configuration (API keys, URLs) |
| `src/meeting/recall_client.py` | Recall.ai API wrapper |
| `src/meeting/meeting_handler.py` | Meeting lifecycle manager |
| `src/meeting/transcription.py` | Transcript processing |
| `src/voice/voice_pipeline.py` | Voice response pipeline |

## Environment requirements

- Rebecca Flask running (default port 5050 locally)
- ngrok running and forwarding to Rebecca
- SSH tunnel to inference_box for Bighead (port 8000)
- Python venv activated with project dependencies
