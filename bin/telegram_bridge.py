#!/usr/bin/env python3
"""Telegram bridge — full bidirectional Claude Code sessions via Telegram.

Each Telegram topic (or chat) maps to a persistent Claude Code session.
Messages relay through `claude -p --resume <session_id>`, maintaining full
conversation history across messages. Works with Telegram Forum Groups (topics)
for per-project threads, or a single private chat.

Usage:
    python3 ~/.claude/bin/telegram_bridge.py

Credentials (loaded from ~/.claude/secrets/telegram.env):
    TELEGRAM_BOT_TOKEN=<token>
    TELEGRAM_CHAT_ID=<@username or numeric id>  (authorized chat only)

Session state: ~/.claude/state/telegram_sessions.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from threading import Thread
import urllib.request
import urllib.error

CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")))
SECRETS_FILE = CLAUDE_HOME / "secrets" / "telegram.env"
SESSIONS_FILE = CLAUDE_HOME / "state" / "telegram_sessions.json"

BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
AUTHORIZED_CHAT: str = os.environ.get("TELEGRAM_CHAT_ID", "")
API_BASE: str = ""


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def load_secrets() -> None:
    global BOT_TOKEN, AUTHORIZED_CHAT, API_BASE
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text().splitlines():
            line = line.strip()
            if "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "TELEGRAM_BOT_TOKEN" and not BOT_TOKEN:
                BOT_TOKEN = v
            elif k == "TELEGRAM_CHAT_ID" and not AUTHORIZED_CHAT:
                AUTHORIZED_CHAT = v
    API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------

def tg(method: str, payload: dict | None = None) -> dict:
    url = f"{API_BASE}/{method}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send(chat_id: int, text: str, thread_id: int | None = None) -> None:
    """Send message, chunking at Telegram's 4096-char limit."""
    for chunk in [text[i:i + 4000] for i in range(0, max(len(text), 1), 4000)]:
        payload: dict = {"chat_id": chat_id, "text": chunk}
        if thread_id:
            payload["message_thread_id"] = thread_id
        resp = tg("sendMessage", payload)
        if not resp.get("ok"):
            print(f"sendMessage failed: {resp.get('error', resp)}", file=sys.stderr, flush=True)


def typing(chat_id: int) -> None:
    tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def is_authorized(chat: dict) -> bool:
    allowed = AUTHORIZED_CHAT.lstrip("@")
    return (
        str(chat.get("id", "")) == allowed
        or chat.get("username", "") == allowed
        or chat.get("username", "") == AUTHORIZED_CHAT.lstrip("@")
    )


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def load_sessions() -> dict:
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except Exception:
        return {}


def save_sessions(sessions: dict) -> None:
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


def session_key(chat_id: int, thread_id: int | None) -> str:
    return f"{chat_id}_{thread_id or 'main'}"


def get_or_create_session(
    chat_id: int,
    thread_id: int | None,
    topic: str | None,
) -> tuple[str, bool]:
    """Return (session_id, is_new). Creates a new UUID session on first use."""
    key = session_key(chat_id, thread_id)
    sessions = load_sessions()
    if key not in sessions:
        new_id = str(uuid.uuid4())
        sessions[key] = {
            "session_id": new_id,
            "project": topic or "main",
            "chat_id": chat_id,
            "thread_id": thread_id,
            "created_at": time.time(),
            "last_active": time.time(),
        }
        save_sessions(sessions)
        return new_id, True
    sessions[key]["last_active"] = time.time()
    save_sessions(sessions)
    return sessions[key]["session_id"], False


def reset_session(chat_id: int, thread_id: int | None, project: str) -> str:
    key = session_key(chat_id, thread_id)
    sessions = load_sessions()
    new_id = str(uuid.uuid4())
    sessions[key] = {
        "session_id": new_id,
        "project": project,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "created_at": time.time(),
        "last_active": time.time(),
    }
    save_sessions(sessions)
    return new_id


# ---------------------------------------------------------------------------
# Claude execution
# ---------------------------------------------------------------------------

def run_claude(session_id: str, message: str, is_new: bool) -> str:
    """Run `claude -p` with session continuity. Returns the text response."""
    # Resolve claude binary — launchd may not have full PATH
    claude_bin = "claude"
    for candidate in [
        "/Users/chadsimon/.local/bin/claude",
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]:
        if Path(candidate).exists():
            claude_bin = candidate
            break

    cmd = [
        claude_bin, "-p",
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
    ]
    if is_new:
        cmd += ["--session-id", session_id]
    else:
        cmd += ["--resume", session_id]
    cmd.append(message)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=os.path.expanduser("~"),
        )
        stdout = result.stdout.strip()
        if stdout:
            try:
                data = json.loads(stdout)
                # json format: {"result": "...", "session_id": "...", ...}
                response = data.get("result") or stdout
                # Strip any residual ANSI escape codes
                import re
                response = re.sub(r"\x1b\[[0-9;]*m", "", response)
                return response.strip()
            except json.JSONDecodeError:
                return stdout
        if result.stderr.strip():
            return f"[Error]: {result.stderr.strip()[:500]}"
        return "[No response received]"
    except subprocess.TimeoutExpired:
        return "[Timed out after 5 minutes. Task may still be running.]"
    except FileNotFoundError:
        return "[claude CLI not found — is Claude Code installed and in PATH?]"
    except Exception as e:
        return f"[Error: {e}]"


# ---------------------------------------------------------------------------
# Command handling
# ---------------------------------------------------------------------------

def handle_message(
    chat_id: int,
    thread_id: int | None,
    topic: str | None,
    text: str,
) -> str:
    text = text.strip()

    # Debug / identity
    if text == "/whoami":
        key = session_key(chat_id, thread_id)
        sessions = load_sessions()
        s = sessions.get(key, {})
        session_snippet = s.get("session_id", "none")[:8] if s else "none"
        return (
            f"Chat ID: {chat_id}\n"
            f"Thread: {thread_id or 'none'}\n"
            f"Topic: {topic or s.get('project', 'main')}\n"
            f"Session: {session_snippet}\n"
            f"Authorized: yes"
        )

    # New / reset session
    if text in ("/new", "/reset") or text.startswith("/new "):
        project = text[5:].strip() if text.startswith("/new ") else (topic or "main")
        new_id = reset_session(chat_id, thread_id, project)
        return f"New session started for '{project}'.\nSession: {new_id[:8]}"

    # List all sessions
    if text == "/sessions":
        sessions = load_sessions()
        if not sessions:
            return "No sessions yet."
        lines = []
        for s in sessions.values():
            age_h = int((time.time() - s.get("last_active", 0)) / 3600)
            lines.append(f"• {s.get('project', '?')}: `{s['session_id'][:8]}` ({age_h}h ago)")
        return "\n".join(lines)

    # Everything else — relay to Claude (including /drive, questions, code)
    session_id, is_new = get_or_create_session(chat_id, thread_id, topic)
    return run_claude(session_id, text, is_new)


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def poll_loop() -> None:
    print(f"Telegram bridge running. Authorized chat: {AUTHORIZED_CHAT}", flush=True)
    offset: int | None = None

    while True:
        try:
            resp = tg("getUpdates", {
                "timeout": 30,
                "offset": offset,
                "allowed_updates": ["message"],
            })

            if not resp.get("ok"):
                err = resp.get("error", "")
                print(f"getUpdates error: {err}", file=sys.stderr, flush=True)
                # 409 = another instance still holds the connection; wait for it to expire
                if "409" in str(err) or "Conflict" in str(err):
                    time.sleep(35)
                else:
                    time.sleep(5)
                continue

            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                if not msg:
                    continue

                chat = msg.get("chat", {})
                if not is_authorized(chat):
                    continue

                text = msg.get("text", "")
                if not text:
                    continue

                chat_id: int = chat["id"]
                thread_id: int | None = msg.get("message_thread_id")

                # Try to extract topic name from forum topic creation message
                topic: str | None = None
                forum_created = (msg.get("reply_to_message") or {}).get("forum_topic_created", {})
                if forum_created:
                    topic = forum_created.get("name")

                def process(chat_id=chat_id, thread_id=thread_id, topic=topic, text=text):
                    typing(chat_id)
                    reply = handle_message(chat_id, thread_id, topic, text)
                    send(chat_id, reply, thread_id)

                Thread(target=process, daemon=True).start()

        except KeyboardInterrupt:
            print("Bridge stopped.", flush=True)
            break
        except Exception as e:
            print(f"Poll error: {e}", file=sys.stderr, flush=True)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    load_secrets()
    if not BOT_TOKEN:
        print(
            "ERROR: TELEGRAM_BOT_TOKEN not set.\n"
            f"Add it to {SECRETS_FILE}",
            file=sys.stderr,
        )
        sys.exit(1)
    if not AUTHORIZED_CHAT:
        print(
            "ERROR: TELEGRAM_CHAT_ID not set.\n"
            f"Add it to {SECRETS_FILE}",
            file=sys.stderr,
        )
        sys.exit(1)
    poll_loop()


if __name__ == "__main__":
    main()
