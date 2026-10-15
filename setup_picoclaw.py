#!/usr/bin/env python3
"""Generate ~/.picoclaw/config.json from environment variables.

Secrets (bot token, API key) stay in Koyeb env vars and are never backed up to
Google Drive: start.sh excludes config.json from the rclone sync, and this
script rebuilds it on every boot.

Required env:
  TELEGRAM_BOT_TOKEN   token from @BotFather
  TELEGRAM_ALLOW_FROM  comma separated numeric Telegram user IDs (from @userinfobot)
  LLM_API_KEY          API key for your LLM provider

Optional env:
  LLM_MODEL            provider/model string      (default: openai/gpt-5.2)
  LLM_MODEL_NAME       alias used by the agent    (default: default)
  LLM_API_BASE         custom OpenAI-compatible base URL
"""
import json
import os
import sys
from pathlib import Path

home = Path(os.environ.get("PICOCLAW_HOME", Path.home() / ".picoclaw"))
home.mkdir(parents=True, exist_ok=True)
cfg_path = home / "config.json"

token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
allow = [x.strip() for x in os.environ.get("TELEGRAM_ALLOW_FROM", "").split(",") if x.strip()]
api_key = os.environ.get("LLM_API_KEY", "").strip()

missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("LLM_API_KEY", api_key)) if not v]
if missing:
    print(f">> [picoclaw] missing env: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)

if not allow:
    # Empty allow_from means *anyone* can talk to the bot and spend your LLM credits.
    print(">> [picoclaw] TELEGRAM_ALLOW_FROM is empty - refusing to start an open bot.", file=sys.stderr)
    sys.exit(1)

model_name = os.environ.get("LLM_MODEL_NAME", "default")
model = {
    "model_name": model_name,
    "model": os.environ.get("LLM_MODEL", "openai/gpt-5.2"),
    "api_key": api_key,
    "request_timeout": 300,
}
if os.environ.get("LLM_API_BASE"):
    model["api_base"] = os.environ["LLM_API_BASE"]

# Start from an existing config (e.g. one made by `picoclaw onboard`) so other
# settings survive, then overwrite only what this script owns.
cfg = {}
if cfg_path.exists():
    try:
        cfg = json.loads(cfg_path.read_text())
    except Exception:
        cfg = {}

cfg.setdefault("agents", {}).setdefault("defaults", {})
cfg["agents"]["defaults"]["workspace"] = str(home / "workspace")
cfg["agents"]["defaults"]["model_name"] = model_name
cfg["model_list"] = [model]
cfg.setdefault("channels", {})["telegram"] = {
    "enabled": True,
    "token": token,
    "allow_from": allow,
}

cfg_path.write_text(json.dumps(cfg, indent=2))
cfg_path.chmod(0o600)
print(f">> [picoclaw] wrote {cfg_path} (telegram allow_from: {len(allow)} user(s))")
