# AGENTS.md: instructions for OpenCode sessions on the Lumen repo

## Owner and communication
- The owner does not write code (the bot was built with AI help). Reply in Russian, in plain words, short: what changed, why, result. Go deeper only when asked. Explain any technical term in a phrase.
- No emojis or heavy markdown in replies to the owner.
- Start every conversational reply with "SilverElixir," (a canary: when it disappears, the owner restarts the session). Never put it in code, file contents, commit messages, or reports.
- Explicit instructions in the owner's task override this file.

## Approvals
- Small requested fixes: do them right away (delegate the edit per "Effort routing").
- Large changes (new dependencies, behavior changes, restructuring, streaming or security layers): short plan first, wait for approval.
- Work on a branch named agent/<short-topic>, never on main. Commit there yourself in small logical steps, one-line message saying what and why, in the language of the recent git log. The owner merges.
- Never push, merge into main, deploy to the HF Space, or contact the live bot without explicit confirmation.

## Secrets
- Never read or print .env files or key values. Some free model tiers are trained on prompts.
- If you find a leaked secret, tell the owner to rotate it. Do not rewrite git history without asking.

## What this repo is
- Lumen: a Telegram bot (persona styled after Claude) running as a single FastAPI + aiogram webhook service on a Hugging Face Space (Docker). LLM backbone: Google Gemini + free OpenRouter models with per-message routing.
- bot.py is the entry point and composition root (env, logging, Dispatcher, main). Domain logic lives in lumen_*.py (admin, message_core, routes, streaming, chat_state, commands, tiktok_flow, rich, media_flow, transport_calls, errors, limits, ...); list the directory to find the module you need.
- Model routing lives in lumen_router_config.py, injection and leak defenses in lumen_security.py.

## Commands (verified against CI, .github/workflows/ci.yml)
- Full gate: pip install -r requirements.txt -r requirements-dev.txt; pyflakes bot.py lumen_*.py system_prompt.py tests/*.py; pytest -q; pip-audit -r requirements.txt
- Single test file: pytest tests/test_bot_routes.py -v; single test: pytest tests/test_bot_routes.py::test_classify_model_error_rate_limit_by_status -v
- Proxy (Deno): deno test proxy/proxy_test.ts (no external imports, works offline; see proxy_test.ts header); deno check proxy/proxy.ts; deno lint proxy/proxy.ts
- Run the gate before changing anything (green baseline) and after each batch of changes. Never say "done" or "tests pass" without having run it.
- If the gate is red before your changes, stop and report which checks fail. Do not start the task and do not fix unrelated failures on your own unless the owner says to continue. A missing dev dependency is the exception: install it.
- Done means: gate green, and the final report lists the changed files and the gate result.

## Gotchas
- Every push to main that passes CI auto-deploys to the live HF Space (.github/workflows/sync-to-hf.yml waits for the CI workflow, then force-pushes the checked commit). A green CI run is a deploy.
- Versions in requirements.txt are pinned with == (HF rebuilds the image from scratch on every deploy; see the file header comment). Bumping a version is a deliberate decision followed by a manual smoke test (/diag + main commands), not a side effect.
- A model leaves the routing only with dated evidence (production log errors or provider docs), written in a comment. _OR_MODEL_HEALTH is the single source of truth for exclusions. Never remove a model on a guess.
- unittest.mock.patch only changes the namespace it targets: patch the module that reads the name (for example lumen_router_config.X), not bot.X.
- Source files contain Cyrillic. Scripts that edit by AST col_offset must work on bytes, because col_offset is a UTF-8 byte offset.

## Code style
- Targeted edits on existing files. Never rewrite a file from scratch unless splitting it was approved.
- Comments: Russian only, except structural headers containing identifiers/commands. Explain why (reason, incident date, tradeoff), max 2-3 lines. Never restate what the code does.
- Add a comment only where non-obvious: magic numbers, regexes, workarounds, ordering constraints, counterintuitive behavior.
- Keep dated evidence comments that prevent regressions, short: (prod 17.09.2026: ...), (audit 26.09.2026), (review 27.09.2026). Move long write-ups to docs/.
- Banned in code, comments and docs: code paraphrase, filler ("Важно", "Здесь мы", "Следует отметить"), filler prose, emojis, heavy dash use, commented-out code.
- Module docstring: one line saying what the module does. No extraction history ("вынесено из bot.py").

## Tests
- Before adding a test, state in the report: what behavior it protects, what real regression makes it fail, why existing tests do not already catch it. If you cannot answer, do not add it.
- Extend an existing parametrized test instead of writing a near-duplicate.
- A bug-fix test must fail on the old code and pass on the fixed code; run both to confirm.
- Do not write: tests without assertions, tests comparing a value to itself, tests where the mock reimplements the asserted behavior, greps over source text.

## Before writing new code
- Check first whether the project, the stdlib or an already installed dependency does it. New dependencies need approval.

## Effort routing
- Answer questions, plans and discussion yourself. Delegate code edits, debugging, audits and multi-file analysis via the subagent tool: mechanical work to `quick`, anything non-trivial to `deep`. If unsure, use `deep`.
- A subagent starts with no chat history. Put into the task text: file paths, the goal, the constraints from this file (branch, no push, gate) and the expected result. Ask it to return a short summary of what changed.
- If the subagents are unavailable, do the work yourself.

## Sentry
- Organization: silverelixir. Call find_organizations first, then search_issues with organizationSlug=silverelixir. Read a single issue via get_sentry_resource with resourceType=issue.

## Docs
- Architecture: docs/ARCHITECTURE.md. Environment variables: docs/ENVIRONMENT.md. Library docs (aiogram, FastAPI, google-genai, Deno): context7 MCP.

## Session hygiene (the owner does not track this, guide them)
- Suggest a new chat when the topic changes completely, and /compact when the session gets long.
