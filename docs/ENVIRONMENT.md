# Environment variables

`BOT_TOKEN` and `GEMINI_API_KEY` are required. Deployments using the authenticated proxy also require `LUMEN_PROXY_SECRET` on both the bot and every proxy instance.

## Required

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | Telegram bot token from @BotFather. `TELEGRAM_TOKEN` and `TELEGRAM_BOT_TOKEN` are also accepted; the first non-empty one wins. |
| `GEMINI_API_KEY` | Google Gemini API key. |

## Telegram & TikWM proxy

Hugging Face Spaces' outbound IPs are blocked by Telegram's API and rejected (`403`) by TikWM. See [Proxy setup](../README.md#proxy-setup) in the main README.

| Variable | Default | Purpose |
|---|---|---|
| `LUMEN_PROXY_SECRET` | — | Required on every Deno proxy and the bot when using proxies. Use the same independently generated random secret (at least 32 random bytes encoded as hex) for primary and fallback instances. Sent only in `X-Lumen-Proxy-Secret`, never in URLs or `Authorization`. |
| `TELEGRAM_API_BASE_URL` | `https://api.telegram.org` | Base URL for the Telegram Bot API. In production this points at the proxy. |
| `TELEGRAM_API_BASE_URL_FALLBACKS` | — | Comma-separated backup proxy addresses. On a circuit-breaker trip the bot rotates through these before pausing. |
| `TG_PROXY_COOLDOWN_SEC` | `20` | Pause (seconds) after the circuit breaker trips, i.e. the proxy is judged unavailable. |
| `TG_PROXY_TRIP_THRESHOLD` | `3` | Consecutive failures (no successes in between) needed to trip the breaker. |
| `TIKWM_API_BASE_URL` | — (direct requests) | Base URL for a TikWM proxy. Empty means the bot talks to both `tikwm.com` mirrors directly. |
| `TIKWM_API_BASE_URL_FALLBACKS` | — | Comma-separated backup TikWM proxies, tried in order if the primary one fails. |

The proxy denies requests before forwarding: missing/wrong credentials return `401`; an unset, empty or invalid server secret returns `503`. Secrets must be printable ASCII without spaces. The header is removed before upstream requests; Telegram bot-token paths and upstream `Authorization` are unchanged. Redirects are rejected to keep requests within the host allowlist. Upstream failures return a generic `502` without exception details. Local serving requires permission to read `LUMEN_PROXY_SECRET` (`--allow-env=LUMEN_PROXY_SECRET`) as well as network permission; isolated tests require neither.

The bot applies proxy authentication to aiogram, raw Telegram/file requests and TikWM metadata requests, including configured fallbacks. The middleware limits credentials to trusted HTTPS proxy origins and base-path boundaries, never direct Telegram/TikWM or media/CDN hosts. Do not put the secret in shared session headers or the TikTok media headers.

Before publishing, configure the same `LUMEN_PROXY_SECRET` on HF and every Deno proxy. Deploy the updated bot client first, then the protected proxy: old clients cannot use the protected proxy. The updated bot refuses configured proxies without a valid secret. Proxy deployment is separate from the HF auto-deploy. The secret is included in log and Sentry redaction.

## Admin access & secrets

| Variable | Default | Purpose |
|---|---|---|
| `ADMIN_SECRET_SEED` | falls back to `BOT_TOKEN`, then to a random value if that's empty too | Salt used to derive `WEBHOOK_SECRET`/`ADMIN_PANEL_KEY`. Set it independently to rotate those two secrets without touching the bot's actual Telegram token. |
| `OWNER_ID` / `BOT_OWNER_ID` / `ADMIN_ID` / `TELEGRAM_OWNER_ID` | — | Telegram user ID that unlocks `/logs` and `/stats`. The first non-empty variable found is used. |
| `BOT_USERNAME` | `LumenAI_bot` | Fallback username; the bot fetches its real one via `getMe` on startup and uses that instead. |

## Models & providers

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` / `OPENROUTER_KEY` | — | OpenRouter API key. Without it, the router sends everything to Gemini, which burns through its much smaller quota fast. |
| `OPENROUTER_HTTP_REFERER` | `https://t.me/{BOT_USERNAME}` | `HTTP-Referer` header sent with OpenRouter requests. |
| `OPENROUTER_TITLE` | `BOT_USERNAME` | App title header sent with OpenRouter requests. |
| `HF_IMAGE_MODEL` | `flux` | Default image-generation model (used only if the prompt doesn't match a more specific style). |

## Timeouts, rate limiting & routing budgets

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_REQUEST_TIMEOUT` | `45s` | Timeout for HTTP calls to the Telegram API. |
| `TELEGRAM_AI_TIMEOUT` | `45s` | Timeout for a single Gemini request outside the main chat route (e.g. `/tts`). |
| `TELEGRAM_MEDIA_TIMEOUT` | `25s` | Timeout for downloading media files from Telegram. |
| `TELEGRAM_GET_FILE_TIMEOUT` | `15s` | Timeout for the `getFile` metadata call before a download. |
| `TTS_MAX_CHARS` | `800` | Max text length accepted by `/tts`. |
| `RATE_LIMIT_MAX_REQUESTS` | `5` | Max requests per user within `RATE_LIMIT_WINDOW_SEC`. |
| `RATE_LIMIT_WINDOW_SEC` | `30s` | Sliding window width for rate limiting. |
| `ROUTE_MODEL_TIMEOUT_SEC` | `22s` | Timeout for a single attempt at a single model. No retries: any failure moves straight to the next model. |
| `ROUTE_TOTAL_BUDGET_SEC` | `40s` | Total time budget for the whole routing chain of one message, across both providers. |
| `DRAW_TOTAL_BUDGET_SEC` | `120s` | Same idea, for the `/draw` fallback chain across image models. |
| `STREAM_CHUNK_TIMEOUT_SEC` | `30s` | Timeout waiting for the next streamed chunk, shared by Gemini and OpenRouter. |
| `FIRST_CHUNK_TIMEOUT_SEC` | `12s` | Floor for waiting on the *first* streamed chunk. The real limit adapts per model (`max(floor, EMA × 2.5)`, see `lumen_model_speed.py`): a usually-fast model hanging once is abandoned early. It only ever shortens the wait — the per-chunk `STREAM_CHUNK_TIMEOUT_SEC` inside the generators remains the ceiling, so the effective first-chunk limit is the minimum of the two. |
| `STREAM_EDIT_MIN_INTERVAL_SEC` | `1.2s` | Minimum interval between message edits during streaming (protects against Telegram's `429`). |
| `STREAM_TYPING_TICK_SEC` | `0.5s` | Interval between steps of the post-stream "catch-up" reveal. |
| `STREAM_TYPING_MAX_CATCHUP_TICKS` | `6` | Max catch-up steps, capping the extra delay this can add. |
| `RICH_MESSAGES_ENABLED` | `1` | Send final answers via `sendRichMessage` (Bot API 10.1+: real tables, headings, math). Any failure falls back to plain HTML automatically; streaming edits always use HTML. Set to `0` (plus restart) to force legacy HTML if rendering breaks on old clients. |

## TikTok downloader

| Variable | Default | Purpose |
|---|---|---|
| `TIKTOK_DOWNLOAD_MAX_BYTES` | `75 MB` | Hard cap on any single downloaded TikTok file (video, slide, or cover), aborted mid-stream if exceeded. |
| `TIKTOK_SLIDE_DOWNLOAD_CONCURRENCY` | `8` | Max slideshow slides downloaded in parallel; keeps one large post from hogging the shared HTTP connection pool. |
| `TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY` | `4` | Max concurrent `ffprobe`/`ffmpeg` processes when probing "live" video slides in a slideshow. |

## Persistent storage

| Variable | Default | Purpose |
|---|---|---|
| `STATE_DIR` | `/app` | Where `chat_state`/`global_quota` files live if Upstash isn't configured. This is the container's ephemeral disk (see [Known limitations](../README.md#known-limitations)). Falls back to a temp directory if not writable. |
| `STATE_FLUSH_CONCURRENCY` | `10` | Max concurrent background writes to storage per flush cycle. |
| `UPSTASH_REDIS_REST_URL` | — | Upstash Redis REST URL. Set together with the token below to persist state across redeploys. |
| `UPSTASH_REDIS_REST_TOKEN` | — | Upstash Redis REST token. |

Setup: create a free database at [upstash.com](https://upstash.com), grab the REST URL and token from the database page, add them as Space secrets, and redeploy. The free tier is 256 MB / 500,000 commands per month, no card required.

## Observability

| Variable | Default | Purpose |
|---|---|---|
| `SENTRY_DSN` | — | Enables Sentry error tracking if set. Every event is scrubbed of known secrets (`BOT_TOKEN`, API keys, `WEBHOOK_SECRET`, etc.) before it's sent. |
| `BOT_LOG_PATH` | `/app/bot.log` | Log file path. Mainly relevant for tests; leave it alone in production. |
| `LOG_LEVEL` | `INFO` | Standard logging level (`DEBUG`/`INFO`/`WARNING`/...). |

Sentry setup: create a free Python project at [sentry.io](https://sentry.io) (Developer tier: 5,000 events/month), copy the DSN from the project settings, and add it as a Space secret.

## Hugging Face Space identity

| Variable | Default | Purpose |
|---|---|---|
| `SPACE_HOST` | computed from the two below | Host used to register the Telegram webhook. Hugging Face usually sets this automatically. |
| `SPACE_AUTHOR_NAME` / `SPACE_REPO_NAME` | `silverelixir` / `lumen` | Used only if `SPACE_HOST` isn't set. |
