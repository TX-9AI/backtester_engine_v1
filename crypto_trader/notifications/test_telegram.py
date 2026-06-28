"""
test_telegram.py — Telegram connectivity test.
v1.0 — 2026-06-27 — initial release, replaces old env-only version

Reads TELEGRAM_TOKEN and TELEGRAM_CHAT_ID from:
  1. Systemd service environment (auto-detected — tries cryptobot then optionsbot)
  2. Shell environment (fallback)

Usage:
  python test_telegram.py
"""

import os
import sys
import subprocess
import requests


def load_systemd_env(*services) -> dict:
    """Try each service name until one returns environment variables."""
    for service in services:
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "show", service, "--property=Environment"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                continue
            line = result.stdout.strip()
            if line.startswith("Environment="):
                line = line[len("Environment="):]
            env = {}
            for part in line.split():
                if "=" in part:
                    key, _, val = part.partition("=")
                    env[key] = val
            if env:
                print(f"  ✓  Loaded credentials from systemd service: {service}")
                return env
        except Exception:
            continue
    return {}


def main():
    print("")
    print("=" * 50)
    print("  Vertigo Capital — Telegram Test")
    print("=" * 50)
    print("")

    svc_env = load_systemd_env("cryptobot", "optionsbot")

    token   = svc_env.get("TELEGRAM_TOKEN")   or os.environ.get("TELEGRAM_TOKEN",   "")
    chat_id = svc_env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")

    if not svc_env:
        print("  →  Using shell environment variables")

    if not token:
        print("  ❌  TELEGRAM_TOKEN not found")
        sys.exit(1)

    if not chat_id:
        print("  ❌  TELEGRAM_CHAT_ID not found")
        sys.exit(1)

    print(f"  Token:   {token[:20]}...")
    print(f"  Chat ID: {chat_id}")
    print("")
    print("  Sending test message...")

    try:
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text":    "✅ Vertigo Capital — Telegram test — connection confirmed",
        }, timeout=10)

        if resp.status_code == 200:
            print("  ✅  Message sent successfully!")
        else:
            print(f"  ❌  Failed: {resp.status_code} — {resp.text}")
            sys.exit(1)

    except Exception as e:
        print(f"  ❌  Error: {e}")
        sys.exit(1)

    print("")


if __name__ == "__main__":
    main()
