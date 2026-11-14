import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

BLOCK_START = "<!-- HERMES-PERMISSIONS:START (auto-generated on every boot, do not edit) -->"
BLOCK_END = "<!-- HERMES-PERMISSIONS:END -->"
PERMISSION_NOTES = f"""{BLOCK_START}
## Permissions & environment (authoritative - overrides anything in memory or old chats)

The owner has granted FULL permissions. Earlier failures like "cron restricted",
"policy restriction", "permission denied" or "needs Active Session / Notification
Permission" were a config problem that is now FIXED. Do NOT repeat them as facts and do
NOT refuse. If something fails, run it, read the real error text, fix the cause and retry;
only report back when it works or when you are truly blocked (then show the exact error).

- `exec` is fully enabled: no deny patterns, no workspace restriction, remote (Telegram) allowed.
- `cron` tool: `action=add` with `command="..."` works from this Telegram chat. Use
  `every_seconds` / `at_seconds` / `cron_expr`. The command's output is posted automatically
  to the SAME Telegram chat. Verify with `cron action=list`.
- Python packages: `pip install <pkg>` is persistent (stored in ~/.picow_clow/pyuser and restored
  from Google Drive on every restart). `requests` and `beautifulsoup4` are already installed.
  apt/npm -g installs are replayed on boot too.
- Keep scripts in {{WORKSPACE}}/scripts/. Anything outside ~/.picoclaw and ~/.picow_clow
  (e.g. /tmp, /app) is LOST on restart. Cron jobs are saved in the workspace and survive restarts.
- Full permission is for getting the job done: never delete the workspace, ~/.picoclaw,
  ~/.picow_clow or the user's data unless explicitly asked.

## OPERATING PROTOCOL (authoritative - follow it on every task)

The model answering you can CHANGE between turns (several models take turns), so nothing lives
"in your head": the plan and progress live in the file {{WORKSPACE}}/tasks/current.md.

### A. Start of every turn
1. If {{WORKSPACE}}/tasks/current.md exists and its Status is not DONE, read it FIRST. It is the
   plan of a job that another model may have started. Continue it; do not restart or re-ask.
2. Read the user's whole message. Split it into REQUIREMENTS: every thing asked, every constraint
   ("don't skip anything", "automatic", "like X"), every result expected. Add the implicit ones:
   it must really run, it must survive a restart, it must be tested.

### B. Plan (jobs with more than 2 steps; plain questions and tiny jobs do not need the file)
Write/update {{WORKSPACE}}/tasks/current.md (move an old finished one to tasks/done/ first):
    # Task: <one-line goal>
    Status: IN-PROGRESS
    ## Requirements   (R1, R2, ... each checkable, in the user's own words)
    - [ ] R1 ...
    ## Plan           (ordered steps)
    ## Log            (one line per finished step: what you ran + the real result)
    ## Assumptions
Then work through the plan step by step and update the file after EACH step (tick the box, add the
evidence). If the user adds or changes something, add new R-lines; never silently drop old ones.

### C. Doing the work
- Act with tools. Never end a turn with a plan, a promise or "now I will ..." - do it in the same turn.
- Do not skip, merge, shorten or postpone anything. No placeholders (TODO, "...", "rest is the same").
  Write complete files and complete commands.
- Understand before you change: read the existing file / config / log first. Make the smallest correct
  change; do not rewrite things that already work.
- Unclear request: first try to resolve it by looking (files, logs, memory). If two readings would give
  different results and looking does not decide, ask ONE short question; otherwise pick the most likely
  reading, write it under Assumptions and continue.
- Big jobs: write big files in several smaller steps. If you run low on tool steps, save the state in
  current.md and tell the user exactly which R-items remain; they will say "continue".
- Tool arguments must be strictly valid JSON.

### D. When something fails (root cause, no guessing)
1. Reproduce it and copy the exact error.  2. Name 2-3 likely causes, most likely first.
3. Test the cheapest one with a command.  4. Fix the cause.  5. Re-run the SAME test, then one
neighbouring check for regressions. After 2 identical failures change the approach. Never retry blindly.

### E. Definition of DONE (gate - pass it before the final message)
- Walk through the Requirements one by one. Each needs [x] AND evidence (command output, file content,
  `cron action=list`). Any [ ] left means: keep working.
- Run it for real. "should work" / "I think" is not evidence; you need the exit code and expected output.
- Only then set "Status: DONE" in current.md.

### F. Final message
Short, in the user's language: for each Rn -> done / not done -> one-line evidence; then where the files
are and what is still open. Be honest about anything untested or not done. No long apologies.

### G. Language & style
Reply in the same language AND script the user writes (Roman Marathi -> Roman Marathi, Devanagari ->
Devanagari). Keep the words short, but "short" is only about wording, never about the amount of work:
if AGENT.md or SOUL.md say "concise" or "fast", that never means skipping steps.
{BLOCK_END}
"""


def write_permission_notes(workspace: Path) -> None:
    """Idempotently (re)write the permissions block in USER.md, keeping the rest of the file."""
    (workspace / "scripts").mkdir(parents=True, exist_ok=True)
    (workspace / "tasks" / "done").mkdir(parents=True, exist_ok=True)  # task ledger, survives model switches
    block = PERMISSION_NOTES.replace("{WORKSPACE}", str(workspace))
    user_md = workspace / "USER.md"
    existing = user_md.read_text(encoding="utf-8") if user_md.exists() else ""
    if BLOCK_START in existing and BLOCK_END in existing:
        head = existing.split(BLOCK_START)[0]
        tail = existing.split(BLOCK_END, 1)[1]
        existing = (head.rstrip() + "\n\n" if head.strip() else "") + tail.lstrip()
    new_text = (existing.rstrip() + "\n\n" if existing.strip() else "") + block
    user_md.write_text(new_text, encoding="utf-8")


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"Error: required environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return val


def wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> None:
    picoclaw_home = Path(
        os.environ.get("PICOCLAW_HOME", os.path.expanduser("~/.picoclaw"))
    ).expanduser()
    workspace = os.environ.get(
        "PICOCLAW_WORKSPACE", str(picoclaw_home / "workspace")
    ).strip()

    picoclaw_home.mkdir(parents=True, exist_ok=True)
    Path(workspace).mkdir(parents=True, exist_ok=True)

    # Remove stale PID files restored from Drive backup so gateway doesn't fail singleton check
    for pid_file in picoclaw_home.rglob("*.pid"):
        try:
            pid_file.unlink()
            print(f"Removed stale PID file: {pid_file}")
        except OSError as exc:
            print(f"Warning: could not remove {pid_file}: {exc}", file=sys.stderr)

    tg_token = require_env("TELEGRAM_BOT_TOKEN")
    allow_from_raw = require_env("TELEGRAM_ALLOW_FROM")
    allow_from = [u.strip() for u in allow_from_raw.split(",") if u.strip()]
    if not allow_from:
        print("Error: TELEGRAM_ALLOW_FROM must contain at least one user ID.", file=sys.stderr)
        sys.exit(1)

    api_key = require_env("LLM_API_KEY")
    model_name = os.environ.get("LLM_MODEL_NAME", "default").strip() or "default"
    model = os.environ.get("LLM_MODEL", "openai/gpt-5.2").strip() or "openai/gpt-5.2"
    upstream_base = os.environ.get("LLM_API_BASE", "").strip() or "https://api.openai.com/v1"

    # Minimum 10 minutes for every LLM request (custom APIs can be slow with a 250k context).
    # The env var can only RAISE it: an old LLM_REQUEST_TIMEOUT=360 on Koyeb must not lower it.
    MIN_TIMEOUT = 600
    try:
        request_timeout = int(os.environ.get("LLM_REQUEST_TIMEOUT", "").strip() or MIN_TIMEOUT)
    except ValueError:
        request_timeout = MIN_TIMEOUT
    request_timeout = max(request_timeout, MIN_TIMEOUT)

    try:
        proxy_port = int(os.environ.get("LOCAL_LLM_PROXY_PORT", "18799").strip())
    except ValueError:
        proxy_port = 18799

    # llm_proxy.py lives next to this script (/app in the image).
    proxy_script = Path(__file__).resolve().with_name("llm_proxy.py")
    if not proxy_script.is_file():
        print(f"Error: {proxy_script} is missing (COPY llm_proxy.py in the Dockerfile).", file=sys.stderr)
        sys.exit(1)

    proxy_env = os.environ.copy()
    proxy_env["PICOCLAW_UPSTREAM_API_BASE"] = upstream_base
    proxy_env["LOCAL_LLM_PROXY_PORT"] = str(proxy_port)
    proxy_env["LLM_REQUEST_TIMEOUT"] = str(request_timeout)
    proxy_env["HERMES_WORKSPACE"] = workspace

    subprocess.Popen(
        [sys.executable, str(proxy_script)],
        env=proxy_env,          # stdout/stderr inherited -> `[guard] model=...` lines show up in Koyeb logs
        start_new_session=True,
    )

    if not wait_for_port("127.0.0.1", proxy_port):
        print("Error: local stream proxy failed to start. Check /tmp/stream_proxy.log", file=sys.stderr)
        sys.exit(1)

    model_entry = {
        "model_name": model_name,
        "model": model,
        "api_key": api_key,
        "api_base": f"http://127.0.0.1:{proxy_port}/v1",
        "request_timeout": request_timeout,
    }

    # Optional: extra JSON fields sent with every request to your custom API, e.g.
    #   LLM_EXTRA_BODY={"reasoning_effort":"high"}
    # (PicoClaw's own `thinking_level` is NOT forwarded to generic OpenAI-compatible endpoints.)
    extra_body_raw = os.environ.get("LLM_EXTRA_BODY", "").strip()
    if extra_body_raw:
        try:
            extra_body = json.loads(extra_body_raw)
            if not isinstance(extra_body, dict):
                raise ValueError("must be a JSON object")
            model_entry["extra_body"] = extra_body
        except ValueError as exc:
            print(f"Warning: LLM_EXTRA_BODY ignored ({exc})", file=sys.stderr)

    def env_int(name: str, default: int) -> int:
        try:
            v = int(os.environ.get(name, "").strip() or default)
            return v if v > 0 else default
        except ValueError:
            return default

    # Context window of the custom API (tokens the model can see). Without this PicoClaw
    # assumes max_tokens*4 (= only ~32k) and summarizes the history far too early.
    context_window = env_int("LLM_CONTEXT_WINDOW", 250000)
    # Max OUTPUT tokens per reply (not the context size).
    max_tokens = env_int("LLM_MAX_TOKENS", 32768)

    cron_allowed_remotes = [
        x.strip()
        for x in os.environ.get("CRON_COMMAND_ALLOWED_REMOTES", "telegram").split(",")
        if x.strip()
    ]

    cfg = {
        "agents": {
            "defaults": {
                "workspace": workspace,
                "model_name": model_name,
                # docs: Security Sandbox -> "Disabling Restrictions"
                "restrict_to_workspace": False,
                "allow_read_outside_workspace": True,
                # let the agent finish multi-step jobs (install -> write script -> cron.add)
                "max_tool_iterations": env_int("LLM_MAX_TOOL_ITERATIONS", 100),
                # sub-agents (default 5 min) get the same >=10 min
                "subturn": {"default_timeout_minutes": MIN_TIMEOUT // 60},
                "context_window": context_window,
                "max_tokens": max_tokens,
            }
        },
        "model_list": [model_entry],
        "channels": {
            "telegram": {
                "enabled": True,
                "token": tg_token,
                "allow_from": allow_from,
                "streaming": {
                    "enabled": True,
                },
            }
        },
        "tools": {
            # path allow-lists (only relevant if restrict_to_workspace is ever turned on)
            "allow_read_paths": ["/"],
            "allow_write_paths": ["/"],
            # docs: Command Execution Settings
            "exec": {
                "enabled": True,                 # master switch
                "allow_remote": True,            # Telegram/Discord/... may run commands
                "enable_deny_patterns": False,   # no built-in 41 deny regexes
                "custom_deny_patterns": [],
                "timeout_seconds": MIN_TIMEOUT,  # default is only 60s; >=10 min
            },
            "cron": {
                "enabled": True,
                "allow_command": True,           # no command_confirm needed
                # Telegram is NOT an "internal" channel in PicoClaw; without this list
                # cron.add(command=...) fails with:
                #   "scheduling command execution is restricted to internal
                #    channels or configured remote channels"
                # Entries: "telegram" (whole channel), "telegram:<chat_id>", or "*".
                "command_allowed_remotes": cron_allowed_remotes,
                "exec_timeout_minutes": MIN_TIMEOUT // 60,
            },
        },
        # docs: Subprocess Isolation (off by default; keep it explicitly off so the
        # agent's commands see the real filesystem, installed packages and $HOME)
        "isolation": {"enabled": False},
    }

    write_permission_notes(Path(workspace))

    config_path = picoclaw_home / "config.json"
    config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"Generated unrestricted PicoClaw config at {config_path} (live stream proxy -> {upstream_base})")
    print(f"context_window={context_window} max_tokens={max_tokens} request_timeout={request_timeout}s "
          f"max_tool_iterations={cfg['agents']['defaults']['max_tool_iterations']}")


if __name__ == "__main__":
    main()
