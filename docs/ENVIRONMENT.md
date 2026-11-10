# Environment variables

`BOT_TOKEN` and `GEMINI_API_KEY` are required. On the Hugging Face Space `ADMIN_SECRET_SEED` is required too: without it the bot refuses to start, because admin keys must not be derived from the bot token. Deployments using the authenticated proxy also require `LUMEN_PROXY_SECRET` on both the bot and every proxy instance.

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
| `ADMIN_SECRET_SEED` | required on HF Spaces; local dev falls back to `BOT_TOKEN`, then to a random value | Salt used to derive `WEBHOOK_SECRET`/`ADMIN_PANEL_KEY`. Set it independently to rotate those two secrets without touching the bot's actual Telegram token. In production (HF Space) an unset seed aborts startup. |
| `OWNER_ID` / `BOT_OWNER_ID` / `ADMIN_ID` / `TELEGRAM_OWNER_ID` | — | Telegram user ID that unlocks `/logs` and `/stats`. The first non-empty variable found is used. |
| `BOT_USERNAME` | `LumenAI_bot` | Fallback username; the bot fetches its real one via `getMe` on startup and uses that instead. |

## Models & providers

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | — | Groq API key. Since 05.11.2026 it is used for one thing only: Whisper transcription of voice notes and audio attachments (`whisper-large-v3-turbo`), which is far cheaper than spending Gemini quota on audio. Without it voice notes go straight to Gemini audio. |
| `MUNITO_API_KEY` | — | Munito AI key. Since 05.11.2026 Munito is the head of every plain-text and image route, with Gemini as reserve; no key means the provider stays invisible to the router and the whole route falls back to the Gemini chains. |
| `MUNITO_ENABLED` | on | Off-switch, not an on-switch: Munito is used while `MUNITO_API_KEY` is set and this value is not `0`/`false`/`off` (plus restart). Emptying the key on the Space has the same effect without a code change. History summarization does not depend on it: `_summarize_text` tries Gemini Flash-Lite first and Munito only as a fallback, so switching Munito off does not turn summaries into a plain cut. |
| `MUNITO_BASE_URL` | `https://api.munito.ai/inference/v1` | OpenAI-compatible endpoint. `api.eu.munito.ai` / `api.us.munito.ai` pin the region — note the US host serves only Qwen and OCR, no DeepSeek or GLM (checked live 05.11.2026). |
| `MUNITO_DAILY_LIMIT` | `250` | Request budget shown in the `/stats` Munito section: the 3M tokens/day free quota divided by ~11K tokens per Lumen request. |
| `MUNITO_REASONING_EFFORT` | `low` | `reasoning_effort` sent with every Munito request (chat and stream): `low`/`minimal` measured 05.11.2026 cut GLM's thinking tokens to zero (~0.2s instead of ~1s) and stop Qwen answering with an empty body. Cost: on a two-step distance problem GLM answered `180` at `low` and `240` at `minimal` (correct is 210) and was right on the rerun, while DeepSeek (route head) was right every time — so expect occasional slips on multi-step tasks, not systematic breakage. Empty string means the field is not sent at all — unknown parameters are rejected with `400`, so never set a value Munito does not list. |
| `GEMINI_DAILY_LIMITS` | — | Per-model Gemini daily limits for `/stats` as `model=limit` pairs (`gemini-2.5-flash=1500`). Models missing here show usage without a limit. |
| `CLOUDFLARE_ACCOUNT_ID` | — | Cloudflare account id for `/draw` (Workers AI). Together with the token below it makes Cloudflare the primary image provider; without either, `/draw` runs on Pollinations alone. Free plan, no card: 10,000 Neurons a day, which is ~96 FLUX.2-klein images at 1024x1024 (measured against the published rates 10.11.2026). |
| `CLOUDFLARE_API_TOKEN` | — | Workers AI API token, sent as `Authorization: Bearer`. Listed among the redacted secrets, so it never reaches `bot.log`, `/logs` or Sentry. |
| `CLOUDFLARE_IMAGE_MODEL` | `@cf/black-forest-labs/flux-2-klein-4b` | Workers AI model id for generation. FLUX.2-klein draws and edits in one model, takes Russian prompts verbatim and accepts a 1024x1024 reference as is; one 1024x1024 image costs 104.2 Neurons (~96 a day). |
| `CLOUDFLARE_EDIT_MODEL` | same as `CLOUDFLARE_IMAGE_MODEL` | Workers AI model id for `/draw`-style edits driven by reference images (`input_image_0`, plus `input_image_1` when the request names two, measured 10.11.2026). Pollinations has no edit mode, so an edit never falls back to it. |
| `CLOUDFLARE_AI_BASE_URL` | `https://api.cloudflare.com/client/v4/accounts` | Workers AI base. `api.cloudflare.com` silently drops connections from Hugging Face egress (prod 09.11.2026: every `/draw` hung to the attempt timeout), so on the Space this must point at the relay instead: `https://<proxy-domain>/fetch/api.cloudflare.com/client/v4/accounts`. The relay only opens the `/client/v4/accounts/<id>/ai/...` path, and a change to that host list has to reach Deno: CI on `main` deploys `proxy/`, but the deploy is conditional, so verify its steps ran (see `docs/ARCHITECTURE.md`, "Deploy paths"). |
| `POLLINATIONS_IMAGE_MODEL` | `sana` | Reserve provider model. Measured 09.11.2026 the free endpoint ignores this parameter entirely and always serves `sana` (`GET /models` returns `["sana"]`); the value is still sent so a future catalog change is visible in `/diag`. The old five-model catalog and the prompt-based style picker were deleted the same day: they selected nothing. sana mangles Cyrillic to `?` while the URL is parsed, so a prompt that is still non-ASCII when it reaches this provider is refused instead of drawing unrelated junk (review 10.11.2026). |
| `IMAGE_APP_REFERRER` | `lumen-telegram-bot` | Sent as `referrer` to Pollinations, which logs the calling app name for keyless traffic. |
| `VOICE_TRANSCRIBE_MAX_BYTES` | `10485760` (10 MB) | Voice/audio attachments larger than this skip Whisper transcription and go straight to Gemini audio, as before. |
| `WEB_SEARCH_ENABLED` | `1` | Free external snippets for every fresh-info request, regardless of whether the route contains a grounded Gemini model. News queries merge DuckDuckGo with Google News (freshness-filtered, publication dates included). Set to `0` (plus restart) to answer from knowledge with an honest "without live search" note instead. |
| `WEB_SEARCH_TIMEOUT_SEC` | `8s` | Cap for the whole external search phase (DuckDuckGo plus RSS fallbacks), inside the shared `ROUTE_TOTAL_BUDGET_SEC`. |
| `WEB_SEARCH_MAX_RESULTS` | `5` | How many snippets are attached to the prompt (each capped, whole block capped). |

## Timeouts, rate limiting & routing budgets

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_REQUEST_TIMEOUT` | `45s` | Timeout for HTTP calls to the Telegram API. |
| `TELEGRAM_AI_TIMEOUT` | `45s` | Budget for the Gemini retry after `MALFORMED_FUNCTION_CALL` inside the main chat route (capped by the remaining `ROUTE_TOTAL_BUDGET_SEC`). Not used by `/tts` — that lives on `TTS_SYNTH_TIMEOUT_SEC`. |
| `TELEGRAM_MEDIA_TIMEOUT` | `25s` | Timeout for downloading media files from Telegram. |
| `TELEGRAM_DOWNLOAD_MAX_BYTES` | `20971520` (20 MB) | Cap on a single Telegram download (Bot API doesn't serve bigger files via `getFile`); bigger files are refused before downloading with an honest user-facing message. |
| `MEDIA_GLOBAL_CONCURRENCY` | `2` | Process-wide cap on simultaneous media downloads (Telegram files, TikTok music/video/slides). Bounds peak memory across chats; per-file caps still apply. |
| `TELEGRAM_GET_FILE_TIMEOUT` | `15s` | Timeout for the `getFile` metadata call before a download. |
| `TELEGRAM_FLOOD_SLEEP_MAX_SEC` | `10s` | Cap on a single `RetryAfter` sleep in Telegram calls, so a long flood-wait cannot hold the chat lock past the route budget (the retry itself is kept). |
| `TTS_MAX_CHARS` | `800` | Size of one `/tts` synthesis chunk; up to `TTS_MAX_PARTS` (5) chunks are synthesized, so the accepted maximum is `TTS_MAX_CHARS × TTS_MAX_PARTS` (4000 by default). |
| `TTS_SYNTH_TIMEOUT_SEC` | `60s` | Timeout for a single TTS synthesis call. Also passed into the SDK as `http_options.timeout`, so a hung provider cannot hold the chat lock. |
| `TTS_TOTAL_BUDGET_SEC` | `240s` | Total deadline for all chunks of one multi-chunk TTS request; past it the text is cut and the user is told only the beginning was voiced. |
| `RATE_LIMIT_MAX_REQUESTS` | `5` | Max requests per user within `RATE_LIMIT_WINDOW_SEC`. |
| `RATE_LIMIT_WINDOW_SEC` | `30s` | Sliding window width for rate limiting. |
| `DAILY_USER_MESSAGE_LIMIT` | `30` | Max model answers per user per day (day boundary: midnight America/Los_Angeles, same as quota). Only successful model answers count; `/reset`/`/lang`/`/start`, failed calls and limit denials don't. `OWNER_ID` is unlimited. |
| `DAILY_USER_GEMINI_LIMIT` | `5` | Of the above, max answers that actually went through Gemini (links, YouTube, video/audio, native grounding). Past it, links/YouTube/video are refused; text and freshness queries are served via Munito, and external search snippets still run for them. The "answered without search" note appears only if that external search also returns nothing. Since 05.11.2026 a Gemini answer counts here again whatever headed the route: the case where it did not was Gemma, which kept answering after this limit off a separate big quota (audit 27.10.2026) and was deleted with the legacy providers. |
| `DAILY_USER_TTS_LIMIT` | `5` | Max `/tts` voicings per user per day (counts toward `DAILY_USER_MESSAGE_LIMIT` too). |
| `DAILY_USER_DRAW_LIMIT` | `5` | Max `/draw` images per user per day (counts toward `DAILY_USER_MESSAGE_LIMIT` too). 5 rather than 3 because drawing is iterative (one picture plus retries) and the scarce resource is guarded by `IMAGE_DAILY_LIMIT`, not by this number. |
| `ROUTE_MODEL_TIMEOUT_SEC` | `22s` | Timeout for a single attempt at a single model. No retries: any failure moves straight to the next model. |
| `MODEL_QUARANTINE_BAD_LIMIT` | `3` | How many consecutive bad responses (empty or garbled) put a model in temporary quarantine until the end of the quota day. A good response resets the counter; the last available model of a route is never quarantined. In-memory only. |
| `ROUTE_TOTAL_BUDGET_SEC` | `40s` | Total time budget for the whole routing chain of one message, across all providers (Gemini and Munito). |
| `MESSAGE_TOTAL_BUDGET_SEC` | `90s` | Ceiling for one message end to end (chat lock wait, external search, routing and post-hoc retry); each stage takes the smaller of its own timeout and the remaining message budget. |
| `DRAW_TOTAL_BUDGET_SEC` | `120s` | Same idea, for the `/draw` chain across image providers (including the wait for a free generation slot). |
| `IMAGE_DAILY_LIMIT` | `80` | Process-wide cap on generated images per quota day. Headroom under the ~96 images FLUX.2-klein-4b allows for 10,000 free Neurons (edits also pay ~21 Neurons per input megapixel); past it `/draw` refuses before calling anyone, instead of every next user re-banging a spent provider. A picture the provider refused on content moderation counts too: the model computed it before the filter rejected the output, so the Neurons are already gone (review 10.11.2026). |
| `IMAGE_GLOBAL_CONCURRENCY` | `2` | Simultaneous generations across all chats. Each accepted image is capped at `IMAGE_MAX_BYTES`, so without this parallel `/draw` calls had no memory ceiling. |
| `IMAGE_QUEUE_TIMEOUT_SEC` | `10s` | How long a `/draw` waits for a free generation slot before saying the service is busy. Deliberately far below `DRAW_TOTAL_BUDGET_SEC`: a person gives up on a picture sooner than a queue would serve it. |
| `DRAW_EDIT_CONTEXT_TTL_SEC` | `600s` | After a generated picture, weak edit verbs ("add a star", "remove the text") apply to that picture without replying to it. Past the window such phrases go to the normal text model, so "make me coffee" never paints over an old photo. |
| `IMAGE_PROVIDER_COOLDOWN_SEC` | `60` | Pause after a provider answers 429 or 402 `FREE_LIMIT`, returns a 5xx through the relay, or hangs/fails at the network level; the provider's own `Retry-After` wins if it is longer. A refusal by content moderation is not a network failure and never parks the provider. Per provider, so the other one keeps working. |
| `IMAGE_ATTEMPT_TIMEOUT_SEC` | `20s` | Timeout for one image provider attempt, well below `DRAW_TOTAL_BUDGET_SEC`: an unreachable host otherwise charged every user the full wait before the reserve answered. |
| `DRAW_PROMPT_PREP_TIMEOUT_SEC` | `8s` | Timeout for rewriting a draw request into a finished image prompt for the provider (ChatGPT parity, 10.11.2026): generation gets a detailed English description, an edit stays an instruction. On failure, timeout or too little budget the user's own wording is sent unchanged. |
| `DRAW_TRANSLATE_TIMEOUT_SEC` | `12s` | Timeout for translating a non-Latin draw prompt to English before generation (sana turns Cyrillic into `?` while parsing the URL, proven 09.11.2026). A failed translation keeps the original prompt; Latin prompts skip the call. |
| `CHAT_LOCK_TIMEOUT_SEC` | `45s` | How long an incoming message waits for that chat's lock before replying "busy". Deliberately shorter than `DRAW_TOTAL_BUDGET_SEC`: a second message during a long drawing gets "busy" instead of hanging. Shared by normal messages, pick-buttons and albums. |
| `STREAM_CHUNK_TIMEOUT_SEC` | `30s` | Timeout waiting for the next streamed chunk, shared by the Gemini and Munito stream generators. |
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
| `TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY` | `2` | Max "live" video slides probed at once in a slideshow — each one runs `ffprobe` and `ffmpeg` in parallel, so the real process ceiling is twice this. |
| `TIKTOK_GROUP_REQUIRE_MENTION` | `0` | Set to `1` so TikTok links in groups without addressing the bot are only recorded as background instead of downloaded. |

## Persistent storage

| Variable | Default | Purpose |
|---|---|---|
| `STATE_DIR` | `/app` | Where `chat_state`/`global_quota` files live if Upstash isn't configured. This is the container's ephemeral disk (see [Known limitations](../README.md#known-limitations)). Falls back to a temp directory if not writable. |
| `STATE_FLUSH_CONCURRENCY` | `10` | Max concurrent background writes to storage per flush cycle. |
| `UPSTASH_REDIS_REST_URL` | — | Upstash Redis REST URL. Set together with the token below to persist state across redeploys. |
| `UPSTASH_REDIS_REST_TOKEN` | — | Upstash Redis REST token. |

Setup: create a free database at [upstash.com](https://upstash.com), grab the REST URL and token from the database page, add them as Space secrets, and redeploy. The free tier is 256 MB / 500,000 commands per month, no card required (limits as of 06.10.2026).

## Hardcoded limits (not environment variables)

These ceilings are fixed in code; change them only with a code edit:

| Constant | Value | Purpose |
|---|---|---|
| `FLUSH_INTERVAL_SEC` | `10` | Period of the background state-flush cycle. |
| `MAX_CHAT_LIMIT` / `PRUNED_CHAT_TARGET` | `5000` / `4500` | Chat-count ceiling and prune target. |
| `MAX_TOTAL_MEMORY_MESSAGES` | `200` | Max stored messages per chat, group background included. |
| `QUOTA_RATE_LIMIT_COOLDOWN_SEC` | `600` | Cooldown after a rate-limit hit. |
| `HISTORY_SUMMARIZE_KEEP` | `150` | Recent messages kept verbatim when older history is summarized. |
| `PHOTO_CONTEXT_WINDOW` / `PHOTO_CONTEXT_MAX` | `30` / `2` | Recent-photo context window and how many photos are attached. |
| `MAX_MEMORY_PENDING` | `400` | Messages waiting for a failed summary before the oldest are dropped. |
| `MEMORY_SUMMARY_RETRY_SEC` | `60` | Delay between retries of a failed summary. |
| `TG_MAX_LEN` | `4096` | Telegram message limit in UTF-16 code units; chunking and HTML fit are measured against it. |
| `TELEGRAM_BOT_API_UPLOAD_LIMIT_BYTES` | `50 MB` | Telegram upload ceiling; bigger variants are skipped before downloading. |
| `WEBHOOK_MAX_BODY_BYTES` | `512 KB` | Biggest accepted webhook update body. |
| `WEBHOOK_MAX_INFLIGHT_TASKS` | `100` | Max background update tasks; overflow asks Telegram to retry (503). |
| `IMAGE_MAX_BYTES` | `30 MB` | Biggest accepted generated image, checked while streaming (a lying `Content-Length` does not pass). |
| `IMAGE_PROMPT_MAX_CHARS` | `600` | Prompt length both image providers accept; longer descriptions are cut and the user is told in the photo caption. |
| `TIKWM_JSON_MAX_BYTES` | `1 MB` | Biggest accepted TikWM metadata body (not the 75 MB media cap). |
| `MIN_PROXY_SECRET_LENGTH` | `32` | Shortest accepted proxy secret, enforced on both sides. |
| `_TABLE_SEP_MAX_LEN` | `500` | Widest table-separator line passed to the separator regex. |
| `_MAX_CHAT_LOCK_WAITERS` | `3` | Max messages of one chat that may wait for the per-chat lock at once; the rest get "busy" immediately so other chats are not starved. |
| `TIKTOK_SLIDESHOW_MAX_ITEMS` | `35` | TikTok slides per post cap (Telegram sends 10 per media group). |
| `MAX_TOTAL_MEMORY_MESSAGES` | `200` | Per-chat ceiling on stored history plus group background lines combined (lumen_memory.py, lumen_chat_state.py). |
| `_ALBUM_EXTRA_MAX_BYTES` | `100 MB` | Cap on the combined size of extra files in a photo album. |
| `_ALBUM_FETCH_CONCURRENCY` | `3` | Parallel downloads inside one album. |

## Observability

| Variable | Default | Purpose |
|---|---|---|
| `SENTRY_DSN` | — | Enables Sentry error tracking if set. Local variables and request bodies are disabled (they can contain prompts/history); every event is scrubbed of known secrets (`BOT_TOKEN`, API keys, `WEBHOOK_SECRET`, etc.) before it's sent. |
| `BOT_LOG_PATH` | `/app/bot.log` | Log file path. Mainly relevant for tests; leave it alone in production. |
| `LOG_LEVEL` | `INFO` | Standard logging level (`DEBUG`/`INFO`/`WARNING`/...). |
| `DIAG_TOTAL_BUDGET_SEC` | `25s` | Total budget for the `/diag` network check across all probed hosts, so a hanging host cannot hang the diagnostic. |
| `DEV_TRACE` | `0` | Set to `1` to log every request, answer and passive group background as JSONL (`devtrace.jsonl` next to `bot.log`), for prompt debugging. Off by default: the file holds chat texts. |
| `DEV_TRACE_PATH` | `<BOT_LOG_PATH dir>/devtrace.jsonl` | Where the dev trace file lives. Point it at a temp dir in tests. |
| `DEV_TRACE_MAX_BYTES` | `5 MB` | File size cap before rotation to a single `.1` backup; garbage values fall back to the default. |
| `ADMIN_SECRET_SEED` | required on HF Spaces | Secret seed for deriving `WEBHOOK_SECRET` and `ADMIN_PANEL_KEY`. **Set it to its own random value, separate from `BOT_TOKEN`.** On production (HF Space) an unset seed aborts startup; local development falls back to `BOT_TOKEN` with a warning. Changing the seed (or setting it for the first time) rotates both keys at once: on restart the bot re-registers the Telegram webhook with the new `WEBHOOK_SECRET` by itself, but anything using the old `ADMIN_PANEL_KEY` (browser bookmarks, cron export scripts) gets `401` until updated — fetch the new key via `GET /admin_keys` with Bearer `BOT_TOKEN`. |

Sentry setup: create a free Python project at [sentry.io](https://sentry.io) (Developer tier: 5,000 events/month as of 06.10.2026), copy the DSN from the project settings, and add it as a Space secret.

## Hugging Face Space identity

| Variable | Default | Purpose |
|---|---|---|
| `SPACE_ID` | set by Hugging Face | Platform-provided Space identifier. Its presence marks the runtime as production for the mandatory `ADMIN_SECRET_SEED` check. |
| `SPACE_HOST` | computed from the two below | Host used to register the Telegram webhook. Hugging Face usually sets this automatically. |
| `SPACE_AUTHOR_NAME` / `SPACE_REPO_NAME` | `silverelixir` / `lumen` | Used only if `SPACE_HOST` isn't set. |
