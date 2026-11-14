#!/usr/bin/env python3
"""Send a Telegram message to every numeric id in TELEGRAM_ALLOW_FROM.

Usage: notify.py "text"
Used by start.sh to report restart / restore / ready status. Never raises: a failed
notification must not break the boot. Disable with NOTIFY_ENABLED=0.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request


def send(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    for attempt in range(4):  # network may not be up for the first seconds of a boot
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404):  # bad token / user never started the bot: don't retry
                print(f"[notify] {chat_id}: HTTP {e.code}", file=sys.stderr)
                return False
        except Exception as e:  # noqa: BLE001
            print(f"[notify] {chat_id}: {e}", file=sys.stderr)
        time.sleep(3 * (attempt + 1))
    return False


def main() -> None:
    if os.environ.get("NOTIFY_ENABLED", "1") == "0" or len(sys.argv) < 2:
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    ids = [x.strip() for x in os.environ.get("TELEGRAM_ALLOW_FROM", "").split(",")]
    ids = [x for x in ids if x.lstrip("-").isdigit()]
    if not token or not ids:
        return
    for chat_id in ids:
        send(token, chat_id, sys.argv[1])


if __name__ == "__main__":
    main()
