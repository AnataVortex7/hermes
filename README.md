# hermes – PicoClaw + Telegram on Koyeb

Container runs `keep_alive.py` (health/dashboard on port 10000) and
**PicoClaw** (`picoclaw gateway`) connected to a Telegram bot via long polling.

## Koyeb environment variables

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather (`/newbot`) |
| `TELEGRAM_ALLOW_FROM` | yes | comma separated numeric user IDs (get yours from @userinfobot). Empty = refuses to start, so the bot is never public |
| `LLM_API_KEY` | yes | key of your LLM provider |
| `LLM_MODEL` | no | `provider/model`, default `openai/gpt-5.2` |
| `LLM_API_BASE` | no | custom OpenAI-compatible endpoint |
| `LLM_REQUEST_TIMEOUT` | no | seconds per LLM request; default and minimum `600` (10 min), can only be raised |
| `LLM_CONTEXT_WINDOW` | no | model context size in tokens, default `250000` |
| `LLM_MAX_TOKENS` | no | max output tokens per reply, default `32768` |
| `LLM_MAX_TOOL_ITERATIONS` | no | max tool calls per turn, default `100` (was 50) |
| `LLM_EXTRA_BODY` | no | JSON object merged into every request to your API, e.g. `{"reasoning_effort":"high"}` (only if your API supports it) |
| `RCLONE_CONFIG_BASE64` | no | Google Drive backup of PicoClaw state |

`~/.picoclaw/config.json` is rebuilt from these on every boot and is **not**
backed up to Drive (it contains secrets). Sessions/memory/workspace are.

## Pin a PicoClaw version
`docker build --build-arg PICOCLAW_VERSION=v0.3.1 .`

## Custom OpenAI-compatible API
Set these env vars (model must keep the `openai/` prefix):

```
LLM_API_BASE=https://my-api.example.com/v1
LLM_MODEL=openai/<your-model-id>
LLM_API_KEY=<your-key>
```

## Tailscale (private access to dashboards from your phone)
Runs in the background in userspace mode (no TUN / NET_ADMIN needed on Koyeb).

| Variable | Notes |
|---|---|
| `TAILSCALE_AUTHKEY` | Tailscale admin -> Settings -> Keys -> Generate auth key. Tick **Reusable** and **Ephemeral** (state is in RAM, so every deploy would otherwise leave a stale device behind) |
| `TAILSCALE_HOSTNAME` | optional, default `hermes` |
| `TAILSCALE_PORTS` | optional, default `10000,9119,18790,18800` - local ports forwarded to your tailnet |

Install the Tailscale app on your phone, log in to the same account, then open
`http://hermes:10000/memory` (or `http://<100.x.y.z>:10000/memory`; the IP is
printed in the Koyeb logs as `[tailscale] connected, IPv4: ...`).

Port 10000 = keep_alive (`/memory`, `/dashboard`), 9119 = Hermes dashboard,
18790 = PicoClaw gateway health, 18800 = PicoClaw web UI (only if something listens there).

## Debugging the model
On boot the log prints `[picoclaw] model=... api_base_host=...`. If it says
`openai/gpt-5.2`, the `LLM_MODEL` env var was not picked up (name must be exactly `LLM_MODEL`).

## Browser (Playwright/Chromium)
Chromium is baked into the image (`PLAYWRIGHT_BROWSERS_PATH=/opt/pw`).
See `examples/browser_example.py` for flags that work in a 512MB container
(`--no-sandbox --disable-dev-shm-usage`, one page at a time, always `browser.close()`).
If it still gets OOM-killed, use `requests` / `curl_cffi` instead.

## Persistent installs
`apt-get` / `pip` / `npm -g` wrappers in `bin/` log packages to
`~/.picow_clow/installed/*.list` (synced to Drive); `start.sh` re-installs them on boot.
Drive sync only starts after a successful restore, so an empty local folder cannot wipe the backup.

## Full permissions (cron + exec from Telegram)
`setup_picoclaw.py` follows https://docs.picoclaw.io/docs/configuration/exec/ and the Security Sandbox page:
`exec.enabled/allow_remote = true`, `enable_deny_patterns = false`, `restrict_to_workspace = false`,
`isolation.enabled = false`, and `tools.cron.command_allowed_remotes = ["telegram"]`
(Telegram is not an "internal" channel, so cron shell commands were rejected without it).
Override with env `CRON_COMMAND_ALLOWED_REMOTES` (e.g. `telegram:123456789` or `*`).
On every boot a marked block is (re)written into `workspace/USER.md` telling the bot it has full
permissions, so stale "cron is restricted" notes in its memory stop making it refuse.
Python packages persist via `PYTHONUSERBASE=~/.picow_clow/pyuser` (synced to Drive).

## Restart / restore notifications
On every boot `start.sh` messages every numeric id in `TELEGRAM_ALLOW_FROM` (you must have pressed
/start on the bot once):
1. `🔄 Server restart ... restore सुरू` - right at boot (PicoClaw is NOT running yet).
2. `✅ PicoClaw ready` - only after the Drive restore succeeded AND the gateway `/ready` returned 200
   (with restore time, restored files, cron job count).
PicoClaw is never started before the restore succeeds. A failed restore is retried (20s apart);
after **2 failed attempts** (`RESTORE_MAX_ATTEMPTS`, default 2) it starts with a **clean history**:
half-restored local files are wiped, a `🧹` message is sent, and Drive sync is disabled for that run so
the old backup on Drive is never overwritten. Restart once Drive works to get the old history back.
Env vars: `NOTIFY_ENABLED=0` (all messages off), `NOTIFY_ON_BOOT=0` (skip the first one),
`RESTORE_MAX_ATTEMPTS=0` or `ALLOW_START_WITHOUT_RESTORE=0` (never start without a restore: retry forever).

## Agent quality guard (round-robin + lite models)
Your custom API rotates several models and ends on `*-lite` ones; the model that answers can change
**every turn**, and lite models are the ones that skip steps, send broken tool JSON or "announce" an
action without doing it. Three layers handle that:

1. **Operating protocol in `workspace/USER.md`** (rewritten on every boot, `setup_picoclaw.py`):
   requirements checklist -> plan -> execute -> verify -> final report per requirement. The plan lives in
   `workspace/tasks/current.md` (synced to Drive), so whichever model answers next can continue the same job.
2. **`llm_proxy.py` (guard proxy on 127.0.0.1:18799)** - validates every agent reply before PicoClaw sees it:
   retries 408/429/5xx; repairs or retries tool calls with invalid JSON / cut off by max_tokens; retries empty
   replies and "Now I will run X:" replies that never call the tool (max 2 retries per request, stall retry max 1);
   fixes Gemini-style parallel tool calls that all arrive with `index 0`; appends a short reminder to the *last*
   message of each agent request (not saved in history), a stronger one when the previous answer came from a lite model.
3. **Logs**: every call prints `[guard] model=<who answered> finish=... reason=ok|empty|stalled|bad_tool_json ... ms=<time> req_kb=<request size>`
   in the Koyeb logs (also `/tmp/stream_proxy.log`), so you can see the round-robin, which model misbehaves, and whether 504s happen when `req_kb` (prompt size) is large.

| Variable | Default | Notes |
|---|---|---|
| `LLM_GUARD` | `1` | `0` = no validation/retry/reminder (proxy only folds SSE and retries HTTP errors) |
| `LLM_REMINDER` | `1` | `0` = do not append the per-request reminder |
| `LLM_GUARD_MAX_RETRIES` | `2` | retries of bad/empty/stalled replies. Each retry is one more call on your model quota - set `1` or `0` if quota is tight |
| `LLM_HTTP_RETRIES` | `3` | retries on 408/429/5xx/connection errors (backoff `LLM_RETRY_BACKOFF`, default 2s, doubling) |
| `LLM_FIRST_BYTE_TIMEOUT` | `600` | seconds (10 min minimum) without any response byte from your API before the call is treated as hung |
| `LLM_STREAM_IDLE_TIMEOUT` | `600` | seconds (10 min minimum) of silence in the middle of a streamed reply before it is treated as hung |
| `LLM_TIMEOUT_RETRIES` | `0` | retries after a hang / 504 / 524 / 408 (`0` = do not send a new request on timeout) |
| `LLM_LITE_PATTERN` | `lite` | regex on the model name returned by your API that switches to the stronger reminder |
| `LLM_PROXY_ACCESS_LOG` | `0` | `1` = also log every HTTP request line |

Tests (no network needed): `python3 -m unittest -v tests.test_llm_proxy`

Note: `Setup Picoclaw.py` (with a space) is an old copy and is not used by the Dockerfile; `setup_picoclaw.py` is the live one.

### 504 although the API logged "200 Success"
`API request failed: Status: 504 Body: error code: 504` is an HTTP reply from the edge (Cloudflare/Koyeb) in front
of your API, not a PicoClaw timer. If one call to a slow model (e.g. 184 s) sends no byte for ~100 s, the edge cuts
the client with 504 while the backend keeps working and later logs 200. Real fix is in the API: open the SSE
response immediately and send a comment line (`: keepalive` + blank line) every ~15 s until the model answers.
The proxy ignores comment lines, so this is safe. By default the proxy waits the full 10 minutes (600 s) without cutting silent calls at 90 s or sending duplicate retries.

## v2 guard fixes (explanations, cut-off replies, streaming)
- **send_file explanation**: PicoClaw's `send_file` result tells the model "provide only a brief confirmation", so the user got a file and no explanation. The proxy now rewrites that sentence in the outgoing request and retries a bare "delivered" reply (`reason=no_explanation`). USER.md section 4 also tells the model to send the explanation with `message` *before* `send_file`.
- **Cut-off replies**: text that ends with `finish_reason=length` (`truncated_text`) and streams that die without `finish_reason`/`[DONE]` (`incomplete_stream`) are retried instead of being delivered as if complete.
- **Streaming bypass**: agent requests (with `tools`) are validated even when PicoClaw asks for `stream=true` (Telegram streaming); the validated reply is re-emitted as SSE. `LLM_GUARD_STREAM=0` turns this off.
- **Temperature**: agent turns use `LLM_AGENT_TEMPERATURE` (default `0.3`; empty = leave PicoClaw's value) for more reliable tool JSON.
- Check in Koyeb logs: agent turns must print `[guard] model=... reason=ok`. If they do not, the guard was being bypassed.

## Typing indicator stays on while working
Telegram hides "typing..." ~5 s after the last `sendChatAction`, so during long LLM calls or long `exec` runs the chat looked dead.
`llm_proxy.py` now re-sends `typing` every 4 s from the moment an agent turn starts, keeps it on while the model asks for tools
(tools run between LLM calls), and stops right after the final answer (or after `TYPING_MAX_IDLE`=720 s of no proxy activity, or on an error).
Uses `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOW_FROM` (private chats). Env: `TYPING_KEEPALIVE=0` off, `TYPING_INTERVAL` (default 4), `TYPING_MAX_IDLE` (default 720).
Limit: it only knows about work that goes through the LLM proxy; background `spawn` sub-agents while the main agent waits without calling the model are not covered.
