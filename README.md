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
