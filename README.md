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
