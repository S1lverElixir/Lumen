---
title: Lumen
colorFrom: yellow
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

<p align="center">
  <img src="assets/lumen-banner.svg" alt="Lumen: Intelligence. Clarity." width="480">
</p>

<p align="center">
  <a href="https://github.com/S1lverElixir/Lumen/actions/workflows/ci.yml"><img src="https://github.com/S1lverElixir/Lumen/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.13-blue.svg" alt="Python 3.13">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
</p>

A Telegram bot styled after Claude's tone and personality (direct, warm, light on hedging), running on Google Gemini and free OpenRouter models with automatic per-message routing between them. Lumen also generates images, downloads TikTok videos without watermarks, and reads text back as speech. It runs as a webhook service on Hugging Face Spaces (Docker).

**Stack:** Python 3.13 · aiogram · FastAPI · google-genai · Docker

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Deploying on Hugging Face Spaces](#deploying-on-hugging-face-spaces)
- [Configuration](#configuration)
- [Proxy setup](#proxy-setup)
- [Diagnostics](#diagnostics)
- [Commands](#commands)
- [Testing](#testing)
- [Known limitations](#known-limitations)
- [Security](#security)
- [License](#license)

> The YAML block above is Hugging Face Space metadata, not a mistake: this same `README.md` doubles as the Space card. Everything below it is normal GitHub documentation.

## Features

- **Automatic model routing.** Every message goes to whichever provider actually has what it needs: web search and link reading go to Gemini, everything else defaults to free OpenRouter models, protecting Gemini's tight daily quota.
- **Streaming replies** with a self-calibrating typing-speed pacer, so answers type themselves in instead of landing in a few large chunks.
- **Defenses against prompt injection and identity leaks.** A deterministic input filter plus output scrubbers keep the bot from revealing which model or provider actually answered.
- **TikTok downloads** without watermarks: video, slideshows (including "live" photo slides), and original sound.
- **Image generation** through Pollinations.ai. The model is picked from the prompt itself (anime, fantasy, realism, quick sketch, or a general default).
- **Text-to-speech** through Gemini TTS.
- `/draw` and `/tts` also work as plain phrases at the start of a message ("draw a cat," "read this out loud"), no slash required.
- **Clarifying buttons** for short taste requests without details ("посоветуй фильм" → genre buttons instead of a guess).
- **Bot language** (`/lang`): system messages in 25 languages (English by default). AI answers always follow the user's own language.
- **Persistent state.** An optional Upstash Redis backend keeps chat history and quota counters alive across redeploys.
- **Error tracking** through an optional Sentry integration that scrubs secrets before sending anything.

## How it works

Lumen is a single FastAPI + aiogram service. Telegram delivers updates to a webhook. Each message gets routed through a chain of candidate models (Gemini and/or OpenRouter), built on the fly from the message's content: attachments, links, and a couple of lightweight heuristics for "does this need current information" and "is this a heavy request." The first provider that answers wins; the other is tried as a fallback if its whole chain fails.

The codebase is a modular monolith. `bot.py` is the entry point and composition root; the rest is split into focused modules: `lumen_admin.py` (FastAPI app, secret gates, admin/diag/export endpoints), `lumen_message_core.py` (message-handling core), `lumen_routes.py` (LLM calls with fallback, `_run_route`), `lumen_streaming.py` (provider-agnostic streaming), `lumen_chat_state.py` (chat state, quotas, locks), `lumen_commands.py` (commands, TTS/draw pipelines, pick buttons), `lumen_tiktok_flow.py` (TikTok send orchestration), `lumen_rich.py` (sending: guests, Rich Messages, chunking), `lumen_media_flow.py` (Telegram attachment downloads), `lumen_transport_calls.py` (sessions, proxy rotation, Telegram calls), `lumen_errors.py` (model error classification), `lumen_limits.py` (rate window, pick queue), `lumen_router_config.py` (model routing), `lumen_formatting.py` (markdown to Telegram HTML, plus Rich Messages for tables/headings/math), `lumen_security.py` (injection and leak defenses), `lumen_message_parse.py` (links, draw/tts triggers, media references), `lumen_media.py` (mime types, file-id parsing), `lumen_images.py`, `lumen_tts.py`, `lumen_tiktok.py`, `lumen_telegram_transport.py`, `lumen_state_storage.py`, `lumen_typing_pace.py`, `lumen_model_speed.py`, and `lumen_lang.py` (bot language: system messages in 25 languages).

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for a deeper look at routing, streaming, the security layers, and the TikTok downloader.

## Deploying on Hugging Face Spaces

1. Push this repository to a Space (`sdk: docker` is already set in the frontmatter above).
2. Set the two required secrets: `BOT_TOKEN` and `GEMINI_API_KEY`.
3. Hugging Face builds the image from `Dockerfile` and runs `python -u bot.py`.
4. On startup the bot registers its own webhook. If that fails, check the logs for `[webhook] setWebhook failed` and register it manually:
   ```bash
   curl -H "Authorization: Bearer <ADMIN_PANEL_KEY>" https://<space-host>/webhook_url
   ```
    then fill the `register_url_template` it returns (token by hand, via curl — never open a token URL in a browser).
5. `WEBHOOK_SECRET` and `ADMIN_PANEL_KEY` are derived deterministically from `BOT_TOKEN` (or from `ADMIN_SECRET_SEED`, if set). Fetch them with:
   ```bash
   curl -H "Authorization: Bearer <BOT_TOKEN>" https://<space-host>/admin_keys
   ```

Hugging Face Spaces' outbound IPs are blocked by Telegram's API at the network level, so a small proxy is required. See [Proxy setup](#proxy-setup) below.

## Configuration

Only `BOT_TOKEN` and `GEMINI_API_KEY` are required. Everything else has a sane default; the full reference (proxy addresses, timeouts, storage, rate limiting, TikTok tuning) lives in **[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)**.

## Proxy setup

Hugging Face Spaces' outbound IPs are blocked by Telegram's Bot API entirely, and TikWM (the TikTok API this bot uses) returns `403` to the same IPs. Both are fixed by one small pass-through proxy, deployed separately on Deno Deploy (source: `proxy/proxy.ts`):

```bash
TELEGRAM_API_BASE_URL=https://<proxy-domain>/fetch/api.telegram.org
TIKWM_API_BASE_URL=https://<proxy-domain>/fetch/www.tikwm.com
```

The proxy only forwards to an explicit host allowlist (`api.telegram.org`, `www.tikwm.com`, `tikwm.com`). See `proxy/proxy.ts` for the implementation and `proxy/proxy_test.ts` for its tests.

## Diagnostics

All three endpoints below require `Authorization: Bearer <ADMIN_PANEL_KEY>` (`/admin_keys`, covered in the deploy steps above, uses `BOT_TOKEN` instead):

| Endpoint | Purpose |
|---|---|
| `GET /diag` | Checks outbound network reachability (Telegram, Gemini, OpenRouter, TikWM, Pollinations, etc.) from inside the container. |
| `GET /webhook_url` | Shows the computed webhook URL and a ready-to-open registration link. |
| `GET /export_state` | Dumps all chat histories and quota counters as JSON, for manual backup. |

`/logs` (a Telegram command, owner-only) sends the current `bot.log` with secrets redacted.

## Commands

`/start`, `/reset`, `/draw`, `/tts`, and `/lang` show up in Telegram's command menu:

| Command | Access | Purpose |
|---|---|---|
| `/start` | anyone | Shows a short intro and the command list. |
| `/reset` | anyone in DMs; group admins/owner in groups | Clears the chat's conversation history. |
| `/draw [description]` | anyone | Generates an image (see [Features](#features)). |
| `/tts [text]` | anyone | Reads text out loud. |
| `/lang` | anyone in DMs; group admins/owner in groups | Bot language menu (system messages; AI answers always follow your language). |

Two more owner-only commands stay out of the menu on purpose, since they surface internal details that shouldn't be visible in a group chat:

| Command | Access | Purpose |
|---|---|---|
| `/stats` | bot owner, DMs only | Active chat count, process uptime, per-model quota usage. |
| `/logs` | bot owner, DMs only | Sends `bot.log` with secrets redacted. |

## Testing

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
```

Test files live in `tests/` and mirror the module split: `test_lumen_formatting.py`, `test_lumen_security.py`, `test_lumen_router_config.py`, `test_lumen_typing_pace.py`, `test_lumen_images.py`, and `test_lumen_model_speed.py` each test their matching module directly, while `test_bot_admin.py`, `test_bot_commands.py`, `test_bot_routes.py`, `test_bot_state.py`, `test_bot_streaming.py`, `test_bot_tiktok.py`, `test_bot_transport.py`, and `test_bot_tts.py` cover `bot.py` by domain (shared fakes live in `bot_test_helpers.py`). `conftest.py` stubs `BOT_TOKEN`/`GEMINI_API_KEY`/`BOT_LOG_PATH` so the suite needs no real secrets.

CI (`.github/workflows/ci.yml`) runs `pyflakes` + `pytest` + `pip-audit` on every push and pull request; a successful run triggers `sync-to-hf.yml`, which mirrors the commit to the Hugging Face Space.

## Known limitations

- Chat history and quota counters survive redeploys only if Upstash is configured (see [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)); otherwise they live on the container's ephemeral disk and reset on every rebuild.
- YouTube downloading isn't supported, because Hugging Face Spaces' outbound IPs are blocked at the TLS handshake level. Viewing and analyzing a video by link still works.
- TikTok downloading depends on the unofficial TikWM API, not an official TikTok endpoint, and needs the proxy above to work from Hugging Face Spaces at all.
- Streaming has only been exercised against mocked clients, not live Gemini/OpenRouter SSE traffic. See the manual smoke-test checklist in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before touching that code path.

## Security

See [SECURITY.md](SECURITY.md) for how to report a vulnerability. Secrets are scrubbed from logs and Sentry events before they leave the container.

## License

MIT — see [LICENSE](LICENSE).
