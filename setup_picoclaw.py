import json
import os
import sys
from pathlib import Path


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"Error: required environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return val


def main() -> None:
    picoclaw_home = Path(
        os.environ.get("PICOCLAW_HOME", os.path.expanduser("~/.picoclaw"))
    ).expanduser()
    workspace = os.environ.get(
        "PICOCLAW_WORKSPACE", str(picoclaw_home / "workspace")
    ).strip()

    picoclaw_home.mkdir(parents=True, exist_ok=True)
    Path(workspace).mkdir(parents=True, exist_ok=True)

    tg_token = require_env("TELEGRAM_BOT_TOKEN")
    allow_from_raw = require_env("TELEGRAM_ALLOW_FROM")
    allow_from = [u.strip() for u in allow_from_raw.split(",") if u.strip()]
    if not allow_from:
        print("Error: TELEGRAM_ALLOW_FROM must contain at least one user ID.", file=sys.stderr)
        sys.exit(1)

    api_key = require_env("LLM_API_KEY")
    model_name = os.environ.get("LLM_MODEL_NAME", "default").strip() or "default"
    model = os.environ.get("LLM_MODEL", "openai/gpt-5.2").strip() or "openai/gpt-5.2"
    api_base = os.environ.get("LLM_API_BASE", "").strip()

    try:
        request_timeout = int(os.environ.get("LLM_REQUEST_TIMEOUT", "360").strip())
    except ValueError:
        request_timeout = 360

    model_entry = {
        "model_name": model_name,
        "model": model,
        "api_key": api_key,
        "request_timeout": request_timeout,
    }
    if api_base:
        model_entry["api_base"] = api_base

    cfg = {
        "agents": {
            "defaults": {
                "workspace": workspace,
                "model_name": model_name,
                "restrict_to_workspace": False,
            }
        },
        "model_list": [model_entry],
        "channels": {
            "telegram": {
                "enabled": True,
                "token": tg_token,
                "allow_from": allow_from,
            }
        },
        "tools": {
            "exec": {
                "enable_deny_patterns": False,
                "allow_remote": True,
            },
            "cron": {
                "allow_command": True,
            },
        },
    }

    config_path = picoclaw_home / "config.json"
    config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"Generated unrestricted PicoClaw config at {config_path}")


if __name__ == "__main__":
    main()