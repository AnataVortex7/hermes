# hermes – PicoClaw + Telegram (Render / Koyeb / any container host)

Container runs `keep_alive.py` (health, live stats and the PicoClaw route on port 10000) and
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

## PicoClaw version is pinned
The Dockerfile installs a fixed release (`ARG PICOCLAW_VERSION=v0.3.1`) from
`github.com/<repo>/releases/download/<tag>/...`, so restarts and redeploys always give the same binary
(`cat /etc/picoclaw-version`). `latest` is rejected on purpose. To upgrade, change that one line and redeploy.

## Live "Agent is working" status (Telegram)
While a job runs, the proxy keeps ONE message updated, with **what exactly** the tool is doing and **why**:

```
⚙️ Agent is working…
🔧 exec — installing pandas, openpyxl
💡 Now I install the libraries to parse the CSV.
Step 27 · 8m
⏱ tool running: 3m
```
- `⏱ …` (new): how long since the model's **last tool call**, so you can see at a glance whether the agent is stuck.
  While a tool runs: `⏱ tool running: 3m`. While it waits for the LLM: `⏱ last tool call: 2m ago` (plus `· waiting for the LLM: 1m`
  once that wait passes a minute - long LLM waits and long-running commands look different). Minute resolution (`<1m`, `5m`, `1h 5m`), so the
  message is still edited only once a minute. Nothing is shown before the first tool call. The "no activity" warning also says how long ago
  the last tool call was.
- `🔧 tool — what`: built from the tool call's name + arguments by plain parsing (no extra LLM request, no API, no tokens):
  `exec` commands become `running scrape.py`, `installing ...`, `downloading host`, `running tests`, `waiting 30s`, `searching ...`;
  file tools become `reading/writing/editing <file>`; `web_search` shows the query, `web_fetch` the host.
- `💡 why`: the first sentence the model already wrote next to its tool call (free); omitted when it wrote none.
- Secrets are masked (tokens, passwords, `Bearer ...`, long keys, `user:pass@`), URLs show only the host.
- While it waits for the LLM: `🧠 Thinking… (last: exec — installing pandas)`; the step count and elapsed time stay (minutes change every
  minute, so a 3-hour job visibly stays alive). When the job ends the message is deleted; if there is no activity for
  `TYPING_MAX_IDLE` (12 min) it becomes a warning and stays.

| Variable | Default | Notes |
|---|---|---|
| `STATUS_MESSAGE` | `1` | `0` = only the typing indicator |
| `STATUS_MIN_INTERVAL` | `3` | min seconds between edits |
| `STATUS_DELETE_ON_FINISH` | `1` | `0` = keep it as `✅ Agent finished (Nm)` |

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
| `TAILSCALE_PORTS` | optional, default `10000,18790,18800` - local ports forwarded to your tailnet |

Install the Tailscale app on your phone, log in to the same account, then open
`http://hermes:10000/memory` (or `http://<100.x.y.z>:10000/memory`; the IP is
printed in the Koyeb logs as `[tailscale] connected, IPv4: ...`).

Port 10000 = keep_alive (`/memory` live stats, `/picoclaw` PicoClaw route),
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

## Dashboards from Telegram + protected server
**Make a dashboard live (any topic):** tell the AI "dashboard live kar". It builds the page in `workspace/dashboards/<name>/`,
runs `hdash start <name> <folder>` and sends you the URL (`http://hermes:81xx/` or `http://100.x.y.z:81xx/`). Open it on any device
that is logged in to your Tailscale - no extra setup. `bin/hdash` only ever publishes ports 8100-8199 (own server: `hdash start n --cmd "python3 app.py --port {port}" --dir folder`).
`hdash list|url|stop|restart`. After a server restart `start.sh` runs `hdash restore`, so dashboards come back with the same URLs (registry is in the workspace, synced to Drive).
Needs the existing `TAILSCALE_AUTHKEY`; no new env var.

**Protected server:** the proxy checks every tool call the model emits and blocks changes to `/app`, `/usr`, `/etc`, Tailscale
(`up/down/serve/set`, `tailscaled`), `~/.picoclaw/config.json`, `.security.yml`, rclone config, killing proxy/gateway, and `rm` of top-level dirs.
The model gets a `BLOCKED` message and is told to use `hdash` or to ask you; if it keeps trying, the call is replaced by a refusal and never reaches PicoClaw.
Reading is allowed. Disable with `PROTECT_SYSTEM=0`. This is a guardrail on what the model asks for (string checks), not an OS sandbox:
a determined obfuscated command could slip through. Real isolation = run the gateway as a non-root user with `/app` read-only (not done here).

## All ports on Tailscale (`tsports`)
Koyeb exposes only one public port (10000). `bin/tsports watch` (started by `start.sh` right after Tailscale connects) scans every 5 s for
TCP ports the server listens on (PicoClaw gateway 18790, web UI 18800, `hdash` dashboards 81xx, anything new) and publishes
each with `tailscale serve --tcp=<port>`; ports that stop listening are removed after ~15 s. An index page with clickable links is on
`http://hermes:8099/`; `tsports list` prints the same in the shell. Excluded by default: 1055 (Tailscale SOCKS), 18799 (LLM proxy, holds the API key), 22, 53 and ports above 32767 (ephemeral).
Env: `TS_EXCLUDE_PORTS`, `TS_MAX_PORT`, `TS_INTERVAL`, `TS_INDEX_PORT` (0 = off). Non-HTTP ports are forwarded too but only open in a browser if they speak HTTP.
Only devices logged in to YOUR tailnet can reach them (nothing is public). Note: a service that listens only on a non-loopback container IP is forwarded to that IP.

## Reliability fixes (typing / missing replies)
- Typing indicator stops as soon as the turn ends with `send_file` (before: it kept typing up to 12 min). After quick tools (read/write file ...) it waits max `TYPING_QUICK_TOOL_IDLE`=150 s for the next request; after `exec`/web/spawn up to `TYPING_MAX_IDLE`=720 s.
- Semantic retries (bad/empty/blocked replies) no longer run past PicoClaw's own request timeout - a late answer after PicoClaw gave up was simply lost.
- Streaming clients (`stream=true`, Telegram streaming) get `: keep-alive` SSE comments every `LLM_SSE_KEEPALIVE`=8 s while the reply is validated, so PicoClaw does not time out on a silent socket. Errors after the first ping are reported in-band.
- The log now says `typing keeper OFF: ...` at start if `TELEGRAM_BOT_TOKEN` / numeric `TELEGRAM_ALLOW_FROM` are missing.

## Long jobs that stop "by themselves" - what was fixed
Per-attempt timeout stays 10 min (`LLM_REQUEST_TIMEOUT`). Jobs of 30+ minutes with hundreds of requests (10 s ... 500 s each) are normal; these were the silent killers:
1. **PicoClaw `max_tool_iterations` = 100** (default here) - a long job simply hit the cap. Now `LLM_MAX_TOOL_ITERATIONS` default **1000**.
2. **Only 3 retries on 429/5xx/connection errors** (~14 s). A short burst of 429s (key pool cooling down) ended the job. Now retries run with backoff (max 30 s) until the request budget is used (`LLM_HTTP_RETRIES`=60).
3. **PicoClaw gave up first.** Proxy retries / re-asks could run past PicoClaw's own `request_timeout` (= 600 s). Now each upstream attempt is still max 10 min, but one PicoClaw request has a total budget `LLM_TOTAL_BUDGET` (default 2x - 60 s = 1140 s) and PicoClaw is told to wait budget+120 s (1260 s).
4. **A hung attempt** (no answer at all, or 408/504/524) is retried once if the budget allows (`LLM_TIMEOUT_RETRIES`=1; set 0 for the old behaviour).
5. **Premature end:** a text-only reply ends a PicoClaw job. If the reply comes in the middle of work (after a tool result) while `tasks/current.md` (fresh, < 45 min) still has `- [ ]` items and no `Status: DONE/BLOCKED/WAITING`, the proxy asks the model once to continue or to state the blocker. Replies that end with a question to the user are never re-asked. Stalled "I will do X:" replies are now re-asked up to `LLM_GUARD_MAX_RETRIES` (2) times (was 1).
6. **Protection false positives** could end a job with a refusal. The system-protection now blocks only real write targets (redirect, `rm/mv/cp/sed -i/tee/chmod ...`, `curl -o`, scripts with write calls, `cd /app && ...`), not mere mentions/reads of `/usr`, `/etc`, `/app`.
7. **Crashes:** the proxy runs under a supervisor loop (restart in 2 s) and `start.sh` restarts the gateway after a crash (`PICOCLAW_MAX_RESTARTS`=20 quick crashes in a row, a run > 10 min resets the counter), with a Telegram notice.
8. **Never a mystery:** when the proxy has to give up (API error after all retries, bad replies after all re-asks, request failure) you get a Telegram message with the reason and the hint to send `continue` (`LLM_NOTIFY_ERRORS=0` to disable, max 1/min). `[guard] ...` lines in the Koyeb log show every decision.
Protocol: "continue" resumes from `tasks/current.md`; a text-only reply ends the job, so the model must call the next tool or write `Status: BLOCKED - <reason>`.

## Final review fixes
- **`dash` renamed to `hdash`**: `/app/bin` is first in PATH, so a helper called `dash` shadowed Debian's real `/usr/bin/dash` shell.
- Requests without a guard (summaries, non-chat endpoints) now also have the retry time budget; before, 60 retries could keep hammering an already rate-limited API for ~30 min after PicoClaw had given up.
- `500/502` retry only `LLM_SERVER_ERROR_RETRIES` (8) times (usually deterministic -> you hear about it early); `429/503`/connection errors keep retrying until the budget is used.
- The "explain before send_file" rule applies only when work was done in the turn (write/edit/exec/...); a plain "send me that file" is not blocked or slowed.
- `tsports`: if `tailscale serve status --json` fails it no longer re-publishes every port on every scan.

## Live stats are now real (`/memory`, `/api/stats`) + `/picoclaw` route
**Why the old numbers were fake:** inside a container `/proc/meminfo`, `/proc/stat`, `/proc/loadavg` and `/proc/uptime` describe the whole *host*. The old page showed host memory against a hard-coded 512 MB, host CPU multiplied by 10, host load average, host uptime and `ps` %mem relative to host RAM.
**Now (`sysstats.py`)**, from the container's own cgroup:
- **RAM**: working set = `memory.current` minus reclaimable page cache (what the OOM killer judges), limit from `memory.max`; also page cache, peak, and **OOM-kill count** (`memory.events`) - a red warning appears if a process was ever killed for memory.
- **CPU**: real usage from `cpu.stat`/`cpuacct.usage` as % of your allotment (cgroup quota, e.g. 0.1 vCPU), avg 1m/5m/15m, and **throttled %** (how much the host held your process back).
- **Processes**: RSS in MB and CPU % of your limit per process (only this container's PIDs). **Uptime** = container uptime (host uptime minus PID 1 start).
- **Disk**: exact `du -x` of `/root`, `/app`, `/tmp` (your data), refreshed every minute in a background thread.
- If the host hides the cgroup, RAM falls back to the sum of process RSS and CPU to the sum of process CPU time; the page footer states the `source` and where each limit came from.
**Set your plan's allotment** (a container cannot always read it): `RAM_LIMIT_MB` (default: cgroup limit, else 512), `CPU_LIMIT` in cores (default: cgroup quota, else 0.1), `DISK_LIMIT_MB` (default 15360 = 15 GB). The disk limit is never visible from inside, so set it to what your plan gives.
Slow work (disk scan, process scan) is in a background thread and the server is threaded, so the platform health check on `/` can no longer be blocked by a stats request.
**`/picoclaw`** replaces the old `/dashboard` (it forwarded to a Hermes dashboard on 9119 that nothing starts any more; removed). `/picoclaw` shows the PicoClaw web UI if something listens on `PICOCLAW_UI_PORT` (18800) - links are rewritten best-effort, WebSockets are not supported through this route (use Tailscale `:18800` for those) - otherwise a built-in status page (gateway `/ready` + `/health`, LLM guard proxy, UI up/down); `/picoclaw/status.json` is the same as JSON. The route is **public** unless you set `UI_BASIC_AUTH=user:password`.

## RAM guard (kills the job that spikes RAM, before Linux OOM does)
`ram_guard.py` (started by `start.sh`, supervised) keeps your **allotted** RAM safe.

**Accurate RAM (your plan, not the host).** Limit = the smallest of `RAM_LIMIT_MB` (you set it), the cgroup limit (all
levels) and `/proc/meminfo` MemTotal (a microVM / sandbox shows only its plan there; ignored if it looks like the host,
> 8 GB or above the cgroup limit). Used = the **higher** of the cgroup working set and system used (MemTotal - MemAvailable).
Check it: `ramguard status` prints the limit, where it came from and both readings. Compare with your host dashboard;
if the limit is wrong or says `UNKNOWN`, set `RAM_LIMIT_MB` (e.g. `512`). The guard also messages you its detected limit at every boot.

**Speed.** Polls every 0.1 s when calm, 0.02 s from 75% and 0.01 s near the limit (about 0.4% of one core calm, ~18 MB RSS).
Besides the 91% line it kills early when the growth rate says RAM would reach ~99% within 0.4 s, and whenever free RAM
would drop under 32 MB (small plans). The guard runs with `oom_score_adj=-1000`, higher priority and `mlockall` (when
allowed) so it is never the OOM victim and never stalls. If Linux still kills something, the guard notices (OOM counter)
and lowers its own threshold by 3 points (min 70%) and tells you.
Measured in a test box (limit 300 MB): a 1 GB/s runaway was cut at ~66%, 250 MB/s at ~81%, slow growth at ~90%.

**What it kills.** At the threshold it picks the agent job that **grew the most in the last 20 s** (no history: the one
**started last**); a job = the command + all its children (a script + its Chromium), shell wrappers are looked through.
It `SIGKILL`s the whole job; if RAM is still high it takes the next culprit (max 3). Jobs under 25 MB that are not growing are never picked.
The event goes to `/tmp/ram_guard/events.jsonl`; `llm_proxy.py` then appends a `[hermes-ram-guard]` message to the agent's
next 3 requests: what was killed, why, and lighter alternatives (requests instead of a browser, chunks/streaming, one
heavy job at a time ...), plus "do not repeat the same command". You also get a Telegram message.

**Never killed:** PicoClaw gateway, keep_alive.py, llm_proxy.py, ram_guard.py, start.sh, tailscaled, rclone, notify.py,
tsports (+ children of those services). If one of those is the biggest process, nothing is killed, only reported (max 1 message / 5 min).

Agent commands: `ramguard status`, `ramguard log`.

| Variable | Default | Notes |
|---|---|---|
| `RAM_GUARD` | `1` | `0` = off |
| `RAM_LIMIT_MB` | auto | your plan's RAM, overrides detection |
| `RAM_GUARD_PERCENT` | `91` | kill threshold, % of the allotted RAM |
| `RAM_GUARD_MIN_FREE_MB` | `32` | also cut when free RAM < this (`0` = off) |
| `RAM_GUARD_WATCH` | `75` | above this, scan processes fast and keep growth history |
| `RAM_GUARD_PREDICT` / `RAM_GUARD_LOOKAHEAD` | `1` / `0.4` | early kill on a runaway growth rate |
| `RAM_GUARD_TICK` | `0.1` | calm polling seconds (0.02 / 0.01 when RAM fills) |
| `RAM_GUARD_MIN_MB` / `RAM_GUARD_MIN_GROW_MB` | `25` / `15` | size / growth needed to count as culprit |
| `RAM_GUARD_PROTECT` | - | extra regex of command lines that must never be killed |
| `RAM_GUARD_NOTIFY` / `RAM_GUARD_MLOCK` | `1` / `1` | Telegram message / lock guard pages in RAM |

A kill is `SIGKILL`: the job cannot save, so the agent is told to continue in small batches.

## Live stats page (`/memory`, JSON at `/api/stats`)
Shows only **this container's plan allotment** (cgroup: RAM working set without page cache, CPU vs the vCPU quota, disk by `du`), never the host machine.
It has red alerts (RAM/CPU >= 85 %, CPU throttled, disk >= 90 %, new OOM kills) and two tables (top by RAM, top by CPU).

Low overhead by design: while nobody has the page/API open the server reads ONE cgroup file every 10 s (to keep the 1m/5m/15m averages)
and nothing else. Opening the page switches to a 2 s sample + process scan; `du` runs at most every 120 s, niced. About 15 s after the last request
it goes idle again. The page pauses its refresh while the browser tab is hidden. Command lines are shown with passwords/keys/tokens redacted.
Tune the limits with `RAM_LIMIT_MB`, `CPU_LIMIT`, `DISK_LIMIT_MB` if auto-detection shows the wrong plan size.
