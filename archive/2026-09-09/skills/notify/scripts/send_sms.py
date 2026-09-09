#!/usr/bin/env python3
"""Send a completion SMS with Twilio's REST API."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")
ACCOUNT_SID_PATTERN = re.compile(r"^AC[a-zA-Z0-9]{32}$")
MAX_BODY_LENGTH = 1600


def read_env(name: str, *, required: bool = True) -> str:
    value = os.getenv(name, "").strip()
    if required and not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def validate_phone(number: str, field_name: str) -> str:
    if not E164_PATTERN.match(number):
        raise ValueError(
            f"{field_name} must be E.164 format (example: +15551234567). Got: {number!r}"
        )
    return number


def validate_account_sid(account_sid: str) -> str:
    if not ACCOUNT_SID_PATTERN.match(account_sid):
        raise ValueError(
            "TWILIO_ACCOUNT_SID must match AC followed by 32 alphanumeric characters."
        )
    return account_sid


def build_message(status: str, task: str, details: str) -> str:
    base = "Codex task completed successfully." if status == "success" else "Codex task failed."
    if task:
        base += f" Task: {task}."
    if details:
        base += f" Details: {details}"
    return base.strip()


def send_sms(account_sid: str, auth_token: str, from_number: str, to_number: str, body: str) -> dict:
    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    payload = urllib.parse.urlencode({"From": from_number, "To": to_number, "Body": body}).encode(
        "utf-8"
    )

    auth_bytes = f"{account_sid}:{auth_token}".encode("utf-8")
    basic_auth = base64.b64encode(auth_bytes).decode("ascii")

    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Twilio API error {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error while calling Twilio: {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send an SMS via Twilio.")
    parser.add_argument("--to", default="", help="Destination number in E.164 format.")
    parser.add_argument("--from", dest="from_number", default="", help="Twilio sender number in E.164 format.")
    parser.add_argument(
        "--status",
        choices=["success", "failure"],
        default="success",
        help="Task outcome used when --body is not provided.",
    )
    parser.add_argument("--task", default="", help="Short task label for the generated message.")
    parser.add_argument("--details", default="", help="Short completion summary for the generated message.")
    parser.add_argument("--body", default="", help="Explicit SMS body. Overrides generated message.")
    parser.add_argument("--dry-run", action="store_true", help="Print request payload without sending an SMS.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        to_number = validate_phone(args.to.strip() or read_env("TWILIO_TO_NUMBER"), "To")
        from_number = validate_phone(
            args.from_number.strip() or read_env("TWILIO_FROM_NUMBER"), "From"
        )
        body = (args.body or build_message(args.status, args.task.strip(), args.details.strip())).strip()
        if not body:
            raise ValueError("Message body cannot be empty.")
        if len(body) > MAX_BODY_LENGTH:
            raise ValueError(
                f"Message body is too long ({len(body)} chars). Maximum is {MAX_BODY_LENGTH}."
            )

        if args.dry_run:
            print(
                json.dumps(
                    {
                        "mode": "dry-run",
                        "to": to_number,
                        "from": from_number,
                        "body": body,
                    },
                    indent=2,
                )
            )
            return 0

        account_sid = validate_account_sid(read_env("TWILIO_ACCOUNT_SID"))
        auth_token = read_env("TWILIO_AUTH_TOKEN")
        result = send_sms(account_sid, auth_token, from_number, to_number, body)
        print(
            json.dumps(
                {
                    "sid": result.get("sid", ""),
                    "status": result.get("status", ""),
                    "to": result.get("to", to_number),
                    "error_code": result.get("error_code"),
                    "error_message": result.get("error_message"),
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
