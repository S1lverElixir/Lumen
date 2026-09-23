# Architecture

This document goes one level deeper than the main [README](../README.md) into how Lumen decides what to do with a message, how streaming works, and how the security layers fit together.

## Request lifecycle

1. Telegram POSTs an update to `/webhook`, authenticated by a secret header (`X-Telegram-Bot-Api-Secret-Token`).
2. `_handle_message_core` in `bot.py` classifies the message: is it a TikTok link, a `/draw` or `/tts` trigger phrase, a reply to earlier media, plain text, or an attachment?
3. `_build_route` (`lumen_router_config.py`) turns that classification into an ordered list of `(provider, model_id)` candidates.
4. `_run_route` tries the first candidate. If a whole provider's chain fails, it falls back to the next provider in the route, except where that's physically impossible (only Gemini can read links or analyze video/audio).
5. The reply streams into the chat if the request qualifies (see [Streaming](#streaming--typing-pace) below), or is sent as a single message otherwise.

## Automatic model routing

Routing is fully automatic: the router builds a fresh candidate list for every message:

- **A YouTube or website link** → Gemini only. It's the only provider that can read page content (`url_context`) or analyze a video by URL.
- **A video or audio attachment** → video goes to Gemini only (OpenRouter's multimodal models only accept images as base64). Voice/audio is first transcribed cheaply (Groq Whisper) and the text joins the normal routing below; if transcription fails or is unavailable, the audio goes to Gemini as before.
- **An image attachment, no live-info need** → free OpenRouter vision models first, Gemini as a reserve.
- **Needs current information** (a lightweight keyword heuristic: "now," "today," "price," "who is currently...") → Gemini, prioritizing the models with a real search-grounding quota, then OpenRouter as a reserve, Groq last (knowledge-only answer if both are down).
- **Plain text, no attachments, no freshness need** (the most common case) → Groq first (1000 free requests/day: Qwen, then gpt-oss), then OpenRouter. Heavier requests (code, multi-step reasoning, caught by another lightweight heuristic) get routed to the stronger free OpenRouter models first.

The reasoning: Gemini's free quota (roughly 20 requests per day for the flagship model) is the scarcest resource in the system, so it's only spent where a capability unique to Gemini is actually needed. Everything else, the bulk of ordinary messages, runs on Groq (1000/day) and OpenRouter's free tiers.

Models that the router should never pick are tracked in a single registry, `_OR_MODEL_HEALTH` (`lumen_router_config.py`), each with a dated reason: the provider dropped the free tier, or the model turned out to be uncensored and a poor fit for Lumen's persona. Nothing is removed on speculation, only on confirmed evidence from production logs (`HTTP 404`, `"no endpoints,"` etc.).

Each model is tried exactly once per route, with no retries: a single failure (timeout, `429`, `5xx`) moves straight to the next candidate. That keeps the worst case bounded by `len(route) × ROUTE_MODEL_TIMEOUT_SEC`, further capped by `ROUTE_TOTAL_BUDGET_SEC` for the whole route.

### Image generation

`/draw` follows the same philosophy on a smaller scale. `_pick_image_model` (`lumen_images.py`) picks a Pollinations.ai model from the prompt's content (anime/manga → `flux-anime`, fantasy → `dreamshaper`, "photorealistic" → `flux-realism`, "quick sketch" → `turbo`, otherwise a general-purpose default) and falls back through the rest of the catalog if the chosen model fails, all within `DRAW_TOTAL_BUDGET_SEC`.

## Streaming & typing pace

For the first candidate in a route (plain text, no attachments, no links), Lumen streams the reply by repeatedly editing one message, the same mechanism for Gemini (`generate_content_stream`), OpenRouter and Groq (SSE over `chat/completions`).

Telegram won't let a bot edit one message more than about once a second, so true token-by-token output isn't possible regardless of how fast the model generates text. Instead, `lumen_typing_pace.py` tracks each `provider:model` pair's real observed characters-per-second and reveals a growing slice of the buffered text at that pace, capped by whatever has actually arrived. If a backend returns the whole answer in one large chunk (common for free OpenRouter models, since the same `:free` slug can be served by different backends depending on load), a short "catch-up" phase finishes revealing it smoothly instead of dumping it all at once, capped at `STREAM_TYPING_MAX_CATCHUP_TICKS × STREAM_TYPING_TICK_SEC` seconds.

This speed estimate is measured, not hardcoded. There is no table of "model X does N tokens/sec" to maintain, because that number isn't a property of the model on OpenRouter's free tier in the first place. A new model just starts at a default speed and calibrates itself over its first few replies.

If the streaming attempt for the head-of-route model fails before showing any text, the bot falls back to a normal (non-streaming) call further down the chain, reusing the same placeholder message rather than sending a new one.

### Manual smoke test before touching streaming code

Streaming is covered only by mocked clients; no test exercises a real Gemini or OpenRouter SSE stream. Before merging changes to `_run_streaming_reply`, `_gemini_stream_pieces`, `_openrouter_stream_pieces`, `_groq_stream_pieces`, or `lumen_typing_pace.py`, check by hand in a real chat:

1. A short message (fits in one edit): once via Gemini, once via OpenRouter.
2. A long message (past `TG_MAX_LEN`): confirm it splits into multiple messages and continues correctly.
3. A request where the streaming model is deliberately unavailable: confirm the silent fallback to a non-streaming reserve model, with no visible glitch.

## Prompt-injection and identity-leak defenses

Lumen deliberately hides which model or provider answers a given message (see the persona in `system_prompt.py`). The system prompt alone is the weakest layer, since any LLM can potentially be talked out of following it with a creative enough injection. So there are several independent layers, each assuming the previous one might have failed:

1. **Input pre-filter** (`_looks_like_injection_probe`, `lumen_security.py`): well-known jailbreak phrasing ("ignore previous instructions," "developer mode," "show me your system prompt") is caught before the model is ever called. The response is fully deterministic.
2. **System prompt** (`system_prompt.py`): instructs the model that anything outside the prompt itself (user messages, chat background, page/video/document content) is data, not instructions, and that the persona doesn't change no matter who claims authority to override it.
3. **Output identity-leak filter** (`_detect_identity_leak` / `_scrub_identity_leak`): a deterministic check on the finished reply, catching exact internal model IDs and narrow self-identification patterns ("I am Gemini," "made by OpenAI"). Deliberately narrow, to avoid false positives on ordinary, honest discussion of other AI companies.
4. **Injected-payload echo filter** (`_detect_injected_payload_echo`): catches the case where an attacker embeds an instruction in a photo or web page ("output this exact string to confirm the jailbreak worked") and the model, while refusing to *follow* it, ends up quoting it back verbatim while summarizing the content.
5. **Garbled-text tripwire** (`_detect_garbled_mix`): flags replies with fragments of unrelated scripts wedged *inside* words (the failure mode of `nemotron-nano-9b-v2`, see `_OR_MODEL_HEALTH`). Log-only (`[mush-suspect]`), never blocks — confirmed cases are reviewed by a human and added to the health registry.

Layers 3 and 4 run before the reply is written to chat history (so a leak can't influence future turns) and, during streaming, before each chunk is shown to the user, not just the final text. Layer 5 runs once on the finished reply (inside the final scrub), for both streaming and non-streaming paths.

None of this is airtight except the input pre-filter. The goal is raising the bar for known attack patterns, not proving immunity to every possible phrasing; incidents get logged with `[identity-leak]` / `[injection-echo]` / `[injection-probe]` / `[mush-suspect]` tags so new patterns can be added as they show up.

## TikTok downloader

Downloads go through the public TikWM API (`tikwm.com`), no account or API key needed. The bot never re-encodes video or photos; bytes go to Telegram exactly as TikWM served them.

- **Video quality**: requested with `&hd=1`; among the variants TikWM returns (`hdplay`/`play`/`wmplay`, each with a known byte size), the best one that fits Telegram's 50 MB upload limit is picked. If Telegram still rejects it as too large, the next lighter variant is tried automatically.
- **Slideshows**: TikTok allows up to 35 slides per post; Telegram's `sendMediaGroup` caps out at 10 items per call. The bot downloads the whole post in parallel and sends it as several media groups in sequence, never with a trailing group of exactly one item (Telegram requires 2–10 per group).
- **"Live" slides**: TikWM's response includes a separate `live_images` field alongside the regular `images`; that's where the actually-moving version of a slide lives, if it has one. Each downloaded slide is also double-checked by its magic bytes (`ftyp` = video container) rather than trusting the field alone.
- TikWM rejects Hugging Face Spaces' outbound IPs with an empty `403`, the same class of block that also keeps [YouTube downloading off the table](../README.md#known-limitations). The proxy (`TIKWM_API_BASE_URL`) is the only fix that's worked in practice; throttling, retries, and spoofed `Referer`/`Origin` headers didn't help on their own (they're still in place as defense in depth).

## Persistent storage

Chat history and quota counters live in `chat_state.json` / `global_quota.json` on the container's local disk by default, which Hugging Face Spaces wipes on every redeploy. If `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` are both set, the same data is written to Upstash Redis instead, and survives redeploys. Each chat gets its own key so one failed write can't take down every chat's state at once, and a small in-memory index tracks which chats exist.

## Error tracking

If `SENTRY_DSN` is set, `sentry_sdk` picks up every `log.exception()`/`log.error()` call already in the codebase with no changes needed at the call sites. Every event is scrubbed of known secrets (`BOT_TOKEN`, API keys, `WEBHOOK_SECRET`, `ADMIN_PANEL_KEY`, the Upstash token) before it leaves the process. Performance tracing is off; this only tracks errors, to stay comfortably inside Sentry's free tier.
