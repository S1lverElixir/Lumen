# AGENTS.md: instructions for OpenCode sessions on the Lumen repo

## Owner and communication
- The owner does not write code (the bot was built with AI help). Reply in Russian, in plain words, short: what changed, why, result. Go deeper only when asked. Explain any technical term in a phrase.
- Start every conversational reply with "SilverElixir," (a canary: when it disappears, the owner restarts the session). Never put it in code, file contents, commit messages, or reports.
- Explicit instructions in the owner's task override this file.

## Approvals
- Small requested fixes: do them right away.
- Large changes (new dependencies, behavior changes, restructuring, streaming or security layers): short plan first, wait for approval.
- Work on a branch. Local commits on a non-main branch are fine. Never push, merge into main, deploy to the HF Space, or contact the live bot without explicit confirmation.

## Secrets
- Never read or print .env files or key values. Some free model tiers are trained on prompts.
- If you find a leaked secret, tell the owner to rotate it. Do not rewrite git history without asking.

## What this repo is
- Lumen: a Telegram bot (persona styled after Claude) running as a single FastAPI + aiogram webhook service on a Hugging Face Space (Docker). LLM backbone: Google Gemini + free OpenRouter models with per-message routing.
- bot.py is the entry point and composition root (env, logging, Dispatcher, main). Domain logic lives in lumen_*.py (admin, message_core, routes, streaming, chat_state, commands, tiktok_flow, rich, media_flow, transport_calls, errors, limits, ...); list the directory to find the module you need.
- Model routing lives in lumen_router_config.py, injection and leak defenses in lumen_security.py.

## Commands (verified against CI, .github/workflows/ci.yml)
- Full gate: pip install -r requirements.txt -r requirements-dev.txt; pyflakes bot.py lumen_*.py system_prompt.py conftest.py test_*.py; pytest -q; pip-audit -r requirements.txt
- Single test file: pytest test_bot_routes.py -v; single test: pytest test_bot_routes.py::test_classify_model_error_rate_limit_by_status -v
- Proxy (Deno): deno test proxy_test.ts (no external imports, works offline; see proxy_test.ts header)
- Run the gate before changing anything (green baseline) and after each batch of changes. Never say "done" or "tests pass" without having run it.

## Gotchas
- Every push to main that passes CI auto-deploys to the live HF Space (.github/workflows/sync-to-hf.yml waits for the CI workflow, then force-pushes the checked commit). A green CI run is a deploy.
- Versions in requirements.txt are pinned with == (HF rebuilds the image from scratch on every deploy; see the file header comment). Bumping a version is a deliberate decision followed by a manual smoke test (/diag + main commands), not a side effect.
- A model leaves the routing only with dated evidence (production log errors or provider docs), written in a comment. _OR_MODEL_HEALTH is the single source of truth for exclusions. Never remove a model on a guess.
- unittest.mock.patch only changes the namespace it targets: patch the module that reads the name (for example lumen_router_config.X), not bot.X.
- Source files contain Cyrillic. Scripts that edit by AST col_offset must work on bytes, because col_offset is a UTF-8 byte offset.

## Code style
- Targeted edits on existing files. Never rewrite a file from scratch unless splitting it was approved.
- Comments explain why, not what. Keep the dated evidence comments that prevent regressions, but keep them short and move long write-ups to docs/.
- No filler prose, emojis, or heavy dash use in code, comments, or docs.

## Session hygiene (the owner does not track this, guide them)
- Effort: for tasks above the default (High: streaming, security, refactor; XHigh: audits, architecture, production incidents), say which level fits and why, then wait for the owner's OK. For default-level tasks (Low: read, explain, rename; Medium: single-module fixes, tests, docs) just proceed. You cannot change effort yourself; the owner does.
- Suggest a new chat when the topic changes completely, and /compact when the session gets long.
