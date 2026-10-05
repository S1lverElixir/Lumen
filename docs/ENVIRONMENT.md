# Environment variables

`BOT_TOKEN` and `GEMINI_API_KEY` are required. Deployments using the authenticated proxy also require `LUMEN_PROXY_SECRET` on both the bot and every proxy instance.

Naming convention (frozen for existing names — renaming a live variable needs a
simultaneous code + platform change, so existing names stay even when imperfect):
new variables are `<SUBSYSTEM>_<THING>` (`TELEGRAM_*`, `TIKWM_*`, `UPSTASH_*`,
`STREAM_*`, `ROUTE_*`). Secrets hold only tokens/keys/passwords; plain numbers,
URLs and hostnames are Variables.

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
| `TELEGRAM_PROXY_COOLDOWN_SEC` | `20` | Pause (seconds) after the circuit breaker trips, i.e. the proxy is judged unavailable. Renamed from `TG_PROXY_COOLDOWN_SEC` (Sept 2026) for a single `TELEGRAM_*` prefix — update the platform entry too, the old name is no longer read. |
| `TELEGRAM_PROXY_TRIP_THRESHOLD` | `3` | Consecutive failures (no successes in between) needed to trip the breaker. Renamed from `TG_PROXY_TRIP_THRESHOLD` (Sept 2026), same as above. |
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
| `OPENROUTER_API_KEY` / `OPENROUTER_KEY` | — | OpenRouter API key. The chain is built regardless of which keys are set, so a missing key means every OpenRouter candidate fails fast with a logged "is not set" rather than being silently routed around. |
| `GROQ_API_KEY` | — | Groq API key (free plan, no card: 1000 req/day). Heads plain-text routes; without it those routes fall through to OpenRouter. Also powers voice transcription (Whisper). |
| `OPENROUTER_DAILY_LIMIT` | `50` | Daily free-model request budget shown in `/stats` (OpenRouter section). Raise it if the plan changes, so the remainder stays honest. |
| `GROQ_DAILY_LIMIT` | `1000` | Same for Groq (free plan: 1000 req/day). |
| `GEMINI_DAILY_LIMITS` | — | Per-model Gemini daily limits for `/stats` as `model=limit` pairs (`gemini-2.5-flash=1500`). Models missing here show usage without a limit. |
| `OPENROUTER_HTTP_REFERER` | `https://t.me/{BOT_USERNAME}` | `HTTP-Referer` header sent with OpenRouter requests. |
| `OPENROUTER_TITLE` | `BOT_USERNAME` | App title header sent with OpenRouter requests. |
| `POLLINATIONS_IMAGE_MODEL` | `flux` | Default image-generation model (used only if the prompt doesn't match a more specific style). Renamed from `HF_IMAGE_MODEL` (Sept 2026) — the backend is Pollinations.ai, not Hugging Face; the old name is no longer read. |
| `VOICE_TRANSCRIBE_MAX_BYTES` | `10485760` (10 MB) | Voice/audio attachments larger than this skip Whisper transcription and go straight to Gemini audio, as before. |

## Timeouts, rate limiting & routing budgets

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_REQUEST_TIMEOUT` | `45s` | Timeout for HTTP calls to the Telegram API. |
| `TELEGRAM_AI_TIMEOUT` | `45s` | Budget for the Gemini retry after `MALFORMED_FUNCTION_CALL` inside the main chat route (capped by the remaining `ROUTE_TOTAL_BUDGET_SEC`). Not used by `/tts` — that lives on `TTS_SYNTH_TIMEOUT_SEC`. |
| `TELEGRAM_MEDIA_TIMEOUT` | `25s` | Timeout for downloading media files from Telegram. |
| `TELEGRAM_DOWNLOAD_MAX_BYTES` | `20971520` (20 MB) | Cap on a single Telegram download (Bot API doesn't serve bigger files via `getFile`); bigger files are refused before downloading with an honest user-facing message. |
| `TELEGRAM_GET_FILE_TIMEOUT` | `15s` | Timeout for the `getFile` metadata call before a download. |
| `TTS_MAX_CHARS` | `800` | Size of one `/tts` synthesis chunk; up to `TTS_MAX_PARTS` (5) chunks are synthesized, so the accepted maximum is `TTS_MAX_CHARS × TTS_MAX_PARTS` (4000 by default). |
| `TTS_SYNTH_TIMEOUT_SEC` | `60s` | Timeout for a single TTS synthesis call. Also passed into the SDK as `http_options.timeout`, so a hung provider cannot hold the chat lock. |
| `TTS_TOTAL_BUDGET_SEC` | `240s` | Total deadline for all chunks of one multi-chunk TTS request; past it the text is cut and the user is told only the beginning was voiced. |
| `RATE_LIMIT_MAX_REQUESTS` | `5` | Max requests per user within `RATE_LIMIT_WINDOW_SEC`. |
| `RATE_LIMIT_WINDOW_SEC` | `30s` | Sliding window width for rate limiting. |
| `DAILY_USER_MESSAGE_LIMIT` | `30` | Max model answers per user per day (day boundary: midnight America/Los_Angeles, same as quota). Only successful model answers count; `/reset`/`/lang`/`/start`, failed calls and limit denials don't. `OWNER_ID` is unlimited. |
| `DAILY_USER_GEMINI_LIMIT` | `5` | Of the above, max answers that actually went through Gemini (links, YouTube, video/audio, fresh data with search). Past it, links/YouTube/video are refused, the rest is served via Groq/OpenRouter (fresh data marked as answered without search). |
| `DAILY_USER_TTS_LIMIT` | `5` | Max `/tts` voicings per user per day (counts toward `DAILY_USER_MESSAGE_LIMIT` too). |
| `ROUTE_MODEL_TIMEOUT_SEC` | `22s` | Timeout for a single attempt at a single model. No retries: any failure moves straight to the next model. |
| `MODEL_QUARANTINE_BAD_LIMIT` | `3` | How many consecutive bad responses (empty or garbled) put a model in temporary quarantine until the end of the quota day. A good response resets the counter; the last available model of a route is never quarantined. In-memory only. |
| `ROUTE_TOTAL_BUDGET_SEC` | `40s` | Total time budget for the whole routing chain of one message, across all providers (Gemini, OpenRouter, Groq). |
| `DRAW_TOTAL_BUDGET_SEC` | `120s` | Same idea, for the `/draw` fallback chain across image models. |
| `CHAT_LOCK_TIMEOUT_SEC` | `45s` | How long an incoming message waits for that chat's lock before replying "busy". Deliberately shorter than `DRAW_TOTAL_BUDGET_SEC`: a second message during a long drawing gets "busy" instead of hanging. Shared by normal messages, pick-buttons and albums. |
| `STREAM_CHUNK_TIMEOUT_SEC` | `30s` | Timeout waiting for the next streamed chunk, shared by Gemini, OpenRouter and Groq. |
| `FIRST_CHUNK_TIMEOUT_SEC` | `12s` | Floor for waiting on the *first* streamed chunk. The real limit adapts per model (`max(floor, EMA × 2.5)`, see `lumen_model_speed.py`): a usually-fast model hanging once is abandoned early. It only ever shortens the wait — the per-chunk `STREAM_CHUNK_TIMEOUT_SEC` inside the generators remains the ceiling, so the effective first-chunk limit is the minimum of the two. |
| `STREAM_EDIT_MIN_INTERVAL_SEC` | `1.2s` | Minimum interval between message edits during streaming (protects against Telegram's `429`). |
| `STREAM_TYPING_TICK_SEC` | `0.5s` | Interval between steps of the post-stream "catch-up" reveal. |
| `STREAM_TYPING_MAX_CATCHUP_TICKS` | `6` | Max catch-up steps, capping the extra delay this can add. |
| `HISTORY_SUMMARY_BUDGET_SEC` | `30s` | Cap for history summarization on overflow; beyond it the history is cut plainly instead of holding the chat lock. |
| `RICH_MESSAGES_ENABLED` | `1` | Send final answers via `sendRichMessage` (Bot API 10.1+: real tables, headings, math). Any failure falls back to plain HTML automatically; streaming edits always use HTML. Set to `0` (plus restart) to force legacy HTML if rendering breaks on old clients. |
| `PICK_BUTTONS_ENABLED` | `1` | Show clarifying buttons for short taste requests without details ("посоветуй фильм"). Set to `0` (plus restart) to answer such requests as plain text instead. |
| `PICK_TTL_SEC` | `300s` | How long pick buttons stay valid. Expired taps on a still-known request reissue the buttons once; anything older answers with a "buttons expired, write in text" notice. |
| `MAX_PENDING_PICKS` | `500` | Cap on simultaneously stored pick-button questions; the oldest are dropped past it (memory only, they do not survive a restart). |
| `MAX_RATE_LIMIT_KEYS` | `20000` | Cap on tracked per-user rate-limit keys; prevents unbounded growth of the in-memory tracker. |
| `INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC` | `10s` | How long shutdown waits for in-flight update tasks before cancelling the rest. |

## TikTok downloader

| Variable | Default | Purpose |
|---|---|---|
| `TIKTOK_DOWNLOAD_MAX_BYTES` | `75 MB` | Hard cap on any single downloaded TikTok file (video, slide, cover or music), aborted mid-stream if exceeded. |
| `TIKTOK_SLIDESHOW_MAX_BYTES` | `200 MB` | Total RAM cap for one slideshow post; slides past it are skipped (a typical post is under 50 MB). |
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

## Hardcoded limits (not environment variables)

These ceilings are fixed in code; change them only with a code edit:

| Constant | Value | Purpose |
|---|---|---|
| `FLUSH_INTERVAL_SEC` | `10` | Period of the background state-flush cycle. |
| `MAX_CHAT_LIMIT` / `PRUNED_CHAT_TARGET` | `5000` / `4500` | Chat-count ceiling and prune target. |
| `MAX_CHAT_HISTORY_LEN` | `100` | Max stored messages per chat. |
| `QUOTA_RATE_LIMIT_COOLDOWN_SEC` | `600` | Cooldown after a rate-limit hit. |
| `HISTORY_SUMMARIZE_KEEP` | `80` | Recent messages kept verbatim when older history is summarized. |

## Observability

| Variable | Default | Purpose |
|---|---|---|
| `SENTRY_DSN` | — | Enables Sentry error tracking if set. Every event is scrubbed of known secrets (`BOT_TOKEN`, API keys, `WEBHOOK_SECRET`, etc.) before it's sent. |
| `BOT_LOG_PATH` | `/app/bot.log` | Log file path. Mainly relevant for tests; leave it alone in production. |
| `LOG_LEVEL` | `INFO` | Standard logging level (`DEBUG`/`INFO`/`WARNING`/...). |
| `DIAG_TOTAL_BUDGET_SEC` | `25s` | Total budget for the `/diag` network check across all probed hosts, so a hanging host cannot hang the diagnostic. |
| `ADMIN_SECRET_SEED` | — | Secret seed for deriving `WEBHOOK_SECRET` and `ADMIN_PANEL_KEY`. **Set it to its own random value, separate from `BOT_TOKEN`.** Without it both keys are derived from `BOT_TOKEN`, so a leaked token also exposes `/export_state` and `/webhook` (the bot logs a warning at startup). Changing the seed (or setting it for the first time) rotates both keys at once: on restart the bot re-registers the Telegram webhook with the new `WEBHOOK_SECRET` by itself, but anything using the old `ADMIN_PANEL_KEY` (browser bookmarks, cron export scripts) gets `401` until updated — fetch the new key via `GET /admin_keys` with Bearer `BOT_TOKEN`. |

Sentry setup: create a free Python project at [sentry.io](https://sentry.io) (Developer tier: 5,000 events/month), copy the DSN from the project settings, and add it as a Space secret.

## Hugging Face Space identity

| Variable | Default | Purpose |
|---|---|---|
| `SPACE_HOST` | computed from the two below | Host used to register the Telegram webhook. Hugging Face usually sets this automatically. |
| `SPACE_AUTHOR_NAME` / `SPACE_REPO_NAME` | `silverelixir` / `lumen` | Used only if `SPACE_HOST` isn't set. |
