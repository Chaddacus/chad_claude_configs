#!/usr/bin/env python3
"""Send a completion WhatsApp message via Meta WhatsApp Cloud API."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

MAX_BODY_LENGTH = 4096
PHONE_NUMBER_ID_PATTERN = re.compile(r"^\d{6,20}$")
API_VERSION_PATTERN = re.compile(r"^v\d+\.\d+$")
RECIPIENT_ALLOWED_PATTERN = re.compile(r"^\+?[0-9()\-\s.]+$")


def read_env(name: str, *, required: bool = True) -> str:
    value = os.getenv(name, "").strip()
    if required and not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def normalize_recipient(value: str, field_name: str) -> str:
    raw = value.strip()
    if raw.lower().startswith("whatsapp:"):
        raw = raw.split(":", 1)[1].strip()

    if not RECIPIENT_ALLOWED_PATTERN.match(raw):
        raise ValueError(
            f"{field_name} contains invalid characters. Use international phone format."
        )

    normalized = "".join(ch for ch in raw if ch.isdigit())
    if not 8 <= len(normalized) <= 15:
        raise ValueError(
            f"{field_name} must be a valid international phone number. Got: {value!r}"
        )
    return normalized


def validate_phone_number_id(value: str) -> str:
    normalized = value.strip()
    if not PHONE_NUMBER_ID_PATTERN.match(normalized):
        raise ValueError(
            "META_WHATSAPP_PHONE_NUMBER_ID must be digits only (usually from Meta app settings)."
        )
    return normalized


def validate_api_version(value: str) -> str:
    normalized = value.strip()
    if not API_VERSION_PATTERN.match(normalized):
        raise ValueError("API version must look like v21.0")
    return normalized


def build_message(status: str, task: str, details: str) -> str:
    base = "Codex task completed successfully." if status == "success" else "Codex task failed."
    if task:
        base += f" Task: {task}."
    if details:
        base += f" Details: {details}"
    return base.strip()


def build_endpoint(api_version: str, phone_number_id: str) -> str:
    return f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"


def send_whatsapp(
    *,
    access_token: str,
    api_version: str,
    phone_number_id: str,
    to_number: str,
    body: str,
) -> dict:
    endpoint = build_endpoint(api_version, phone_number_id)
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        endpoint,
        data=payload_bytes,
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"WhatsApp Cloud API error {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error while calling WhatsApp Cloud API: {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a WhatsApp message via WhatsApp Cloud API.")
    parser.add_argument("--to", default="", help="Destination number (e.g. +15551234567).")
    parser.add_argument(
        "--phone-number-id",
        default="",
        help="Meta WhatsApp phone number ID. Overrides META_WHATSAPP_PHONE_NUMBER_ID.",
    )
    parser.add_argument(
        "--api-version",
        default="",
        help="Graph API version (e.g. v21.0). Overrides META_WHATSAPP_API_VERSION.",
    )
    parser.add_argument(
        "--status",
        choices=["success", "failure"],
        default="success",
        help="Task outcome used when --body is not provided.",
    )
    parser.add_argument("--task", default="", help="Short task label for the generated message.")
    parser.add_argument("--details", default="", help="Short completion summary for the generated message.")
    parser.add_argument("--body", default="", help="Explicit WhatsApp body. Overrides generated message.")
    parser.add_argument("--dry-run", action="store_true", help="Print payload without sending a message.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        to_number = normalize_recipient(args.to or read_env("WHATSAPP_TO_NUMBER"), "To")
        phone_number_id = validate_phone_number_id(
            args.phone_number_id or read_env("META_WHATSAPP_PHONE_NUMBER_ID")
        )
        api_version = validate_api_version(
            args.api_version or os.getenv("META_WHATSAPP_API_VERSION", "v21.0")
        )

        body = (args.body or build_message(args.status, args.task.strip(), args.details.strip())).strip()
        if not body:
            raise ValueError("Message body cannot be empty.")
        if len(body) > MAX_BODY_LENGTH:
            raise ValueError(
                f"Message body is too long ({len(body)} chars). Maximum is {MAX_BODY_LENGTH}."
            )

        endpoint = build_endpoint(api_version, phone_number_id)
        preview_payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }

        if args.dry_run:
            print(
                json.dumps(
                    {
                        "mode": "dry-run",
                        "endpoint": endpoint,
                        "payload": preview_payload,
                    },
                    indent=2,
                )
            )
            return 0

        access_token = read_env("META_WHATSAPP_ACCESS_TOKEN")
        result = send_whatsapp(
            access_token=access_token,
            api_version=api_version,
            phone_number_id=phone_number_id,
            to_number=to_number,
            body=body,
        )
        messages = result.get("messages") if isinstance(result, dict) else None
        message_id = ""
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            message_id = str(messages[0].get("id", ""))

        print(
            json.dumps(
                {
                    "message_id": message_id,
                    "to": to_number,
                    "api_version": api_version,
                },
                indent=2,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
