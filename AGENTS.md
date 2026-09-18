# AGENTS.md — instructions for OpenCode sessions on the Lumen repo

## Language and user context
- The user does not write code (the bot was built with Claude's help). Explain in Russian, in detail: what was done, why, and the result in plain terms.

## Agreement with the owner
- Small fixes and requested changes: do them right away. Large changes (new dependencies, behavior changes, restructuring, touching streaming or security layers): present a short plan first and get the owner's approval.
- Publishing (git commit/push, deploy to the HF Space, contacting the live bot): propose it, wait for explicit confirmation, never do it unprompted.

## What this repo is
- Lumen: a Telegram bot (persona styled after Claude) running as a single FastAPI + aiogram webhook service on a Hugging Face Space (Docker). LLM backbone: Google Gemini + free OpenRouter models with per-message routing.
- Entry point is bot.py (orchestrator, ~5000 lines). Focused modules sit next to it: lumen_router_config.py (model routing), lumen_formatting.py (markdown to Telegram HTML), lumen_security.py (injection/leak defenses), lumen_message_parse.py (links, draw/tts triggers, media references), lumen_images.py, lumen_tts.py, lumen_tiktok.py, lumen_telegram_transport.py, lumen_state_storage.py, lumen_typing_pace.py, lumen_model_speed.py (measured model latency), plus system_prompt.py.

## Commands (verified against CI, .github/workflows/ci.yml)
- Full gate: pip install -r requirements.txt -r requirements-dev.txt; pyflakes bot.py lumen_*.py system_prompt.py conftest.py test_*.py; pytest -q; pip-audit -r requirements.txt
- Single test file: pytest test_bot.py -v; single test: pytest test_bot.py::test_classify_model_error_rate_limit_by_status -v
- Proxy (Deno): deno test proxy_test.ts (no external imports, works offline; see proxy_test.ts header)

## Gotchas
- Every push to main that passes CI auto-deploys to the live HF Space (.github/workflows/sync-to-hf.yml waits for the CI workflow, then force-pushes the checked commit). A green CI run is a deploy.
- Versions in requirements.txt are pinned with == (HF rebuilds the image from scratch on every deploy; see the file header comment). Bumping a version is a deliberate decision followed by a manual smoke test (/diag + main commands), not a side effect.

## Session hygiene (guide the owner proactively, they don't track this)
- Effort: at the start of each task, state which effort fits and why, then proceed on their OK. Low: read/explain/rename; Medium (default): single-module fixes, tests, docs; High: streaming/security/refactor; XHigh: audits, architecture, prod incidents.
- New chat: suggest starting one when the topic changes completely or the session mixes several unrelated tasks.
- Compactness: suggest `/compact` (or a fresh chat) when the session gets long — many files read, repeated re-reads, or signs of lost context.
