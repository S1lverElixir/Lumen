# AGENTS.md: instructions for sessions on the Lumen repo

## Owner and communication
- The owner does not write code (the bot was built with AI help). Reply in Russian, in plain words, short: what changed, why, result. Go deeper only when asked. Explain any technical term in a phrase.
- No emojis or heavy markdown in replies to the owner.
- Start every conversational reply with "SilverElixir," (a canary: when it disappears, the owner restarts the session). Never put it in code, file contents, commit messages, or reports.
- Explicit instructions in the owner's task override this file, except Approvals and Secrets: push, merge, deploy, contact with the live bot and access to secrets need direct confirmation of that specific action.
- If a requirement is ambiguous, ask one short question instead of guessing. If the answer changes little, pick the simpler option and state the assumption in the report.

## Approvals
- Small requested fixes: do them right away.
- Large changes (new dependencies, behavior changes, restructuring, streaming or security layers): short plan first, wait for approval.
- Protected files: system_prompt.py, lumen_security.py, lumen_router_config.py, .github/workflows/*, Dockerfile. Change them only after a plan is approved, even for a one-line fix.
- Work on a branch named agent/<short-topic>, never on main. Commit there yourself in small logical steps, one-line message saying what and why, in the language of the recent git log. The owner merges.
- Never push, merge into main, deploy to the HF Space, or contact the live bot without explicit confirmation.

## Secrets
- Never read or print .env files or key values. Some free model tiers are trained on prompts.
- Never call the live admin endpoints (/admin_keys, /export_state, /diag, /webhook_url). /admin_keys returns the service keys, /export_state dumps all chat histories (private user data). Do not paste their output anywhere.
- If you find a leaked secret, tell the owner to rotate it. Do not rewrite git history without asking.

## What this repo is
- Lumen: a Telegram bot (persona styled after Claude) running as a single FastAPI + aiogram webhook service on a Hugging Face Space (Docker, Python 3.13, entry: python -u bot.py). LLM backbone: Google Gemini + free OpenRouter models with per-message routing (web search and link reading go to Gemini, the rest to free OpenRouter models, to protect Gemini's daily quota).
- bot.py is the entry point and composition root (env, logging, Dispatcher, main). Domain logic lives in lumen_*.py; list the directory to find the module you need.
- Model routing lives in lumen_router_config.py, injection and leak defenses in lumen_security.py.
- Tests need no real secrets: conftest.py stubs BOT_TOKEN, GEMINI_API_KEY and BOT_LOG_PATH. Do not add real keys to tests or fixtures.

## Commands (verified against CI, .github/workflows/ci.yml)
- Setup, once per session: pip install -r requirements.txt -r requirements-dev.txt
- Gate: pyflakes bot.py lumen_*.py system_prompt.py tests/*.py; pytest -q
- Proxy gate, required when proxy/ is touched, otherwise skip: deno test proxy/proxy_test.ts (no external imports, works offline; see proxy_test.ts header); deno check proxy/proxy.ts; deno lint proxy/proxy.ts
- Dependency audit: pip-audit -r requirements.txt. CI runs it too, but it needs network and goes red when a new CVE appears in the pinned versions. Run it only when touching requirements.txt or when the owner asks; it is not part of the baseline.
- Single test file: pytest tests/<file>.py -v; single test: pytest tests/<file>.py::<test_name> -v
- Do not start the bot locally. It needs live tokens and webhooks. Verification is the gate; live checks (/diag + main commands) are done by the owner after deploy.
- Run the gate before changing anything (green baseline) and after each batch of changes.
- If the gate is red before your changes, stop and report which checks fail. Do not start the task and do not fix unrelated failures unless the owner says to continue. A missing dev dependency is the exception: install it.
- Done means: the gate was actually run and is green, and the final report is written. Never say "done" or "tests pass" otherwise.

## Final report
- Short, in this order: what changed and why; changed files; gate result (and proxy gate if run); what was not verified; risks or things the owner must check by hand (for example a smoke test after a version bump or a routing change).
- If nothing is unverified or risky, say so in one phrase instead of padding.

## Gotchas
- Every push to main that passes CI auto-deploys to the live HF Space (.github/workflows/sync-to-hf.yml waits for the CI workflow, then force-pushes the checked commit). A green CI run is a deploy.
- The Telegram proxy (proxy.ts, Deno Deploy) is deployed separately from the HF Space. Pushing a change to it does not update the running proxy: after any proxy change, tell the owner it needs a manual redeploy.
- Streaming has only been tested against mocks, not live Gemini/OpenRouter SSE. Before touching streaming code, read the manual smoke-test checklist in docs/ARCHITECTURE.md and list in the report which of its points the owner must run by hand.
- Versions in requirements.txt are pinned with == (HF rebuilds the image from scratch on every deploy; see the file header comment). Bumping a version is a deliberate decision followed by a manual smoke test (/diag + main commands), not a side effect.
- A model leaves the routing only with dated evidence (production log errors or provider docs), written in a comment. _OR_MODEL_HEALTH is the single source of truth for exclusions. Never remove a model on a guess.
- unittest.mock.patch only changes the namespace it targets: patch the module that reads the name (for example lumen_router_config.X), not bot.X.
- Source files contain Cyrillic. Scripts that edit by AST col_offset must work on bytes, because col_offset is a UTF-8 byte offset.

## Code style
- Targeted edits on existing files. Never rewrite a file from scratch unless splitting it was approved.
- Comments: Russian only, except structural headers containing identifiers/commands. Explain why (reason, incident date, tradeoff), max 2-3 lines. Never restate what the code does.
- Add a comment only where non-obvious: magic numbers, regexes, workarounds, ordering constraints, counterintuitive behavior.
- Keep dated evidence comments that prevent regressions, short: (prod ДД.ММ.ГГГГ: ...), (audit ДД.ММ.ГГГГ), (review ДД.ММ.ГГГГ). Move long write-ups to docs/.
- Banned in code, comments and docs: code paraphrase, filler ("Важно", "Здесь мы", "Следует отметить"), emojis, heavy dash use, commented-out code.
- Module docstring: one line saying what the module does. No extraction history ("вынесено из bot.py").

## Tests
- Before adding a test, state in the report: what behavior it protects, what real regression makes it fail, why existing tests do not already catch it. If you cannot answer, do not add it.
- Extend an existing parametrized test instead of writing a near-duplicate.
- A bug-fix test must fail on the old code and pass on the fixed code; run both to confirm.
- Do not write: tests without assertions, tests comparing a value to itself, tests where the mock reimplements the asserted behavior, greps over source text.

## Before writing new code
- Check first whether the project, the stdlib or an already installed dependency does it. New dependencies need approval.

## Sentry
- Organization: silverelixir. Call find_organizations first, then search_issues with organizationSlug=silverelixir. Read a single issue via get_sentry_resource with resourceType=issue.

## Docs
- Architecture: docs/ARCHITECTURE.md. Environment variables: docs/ENVIRONMENT.md. Library docs (aiogram, FastAPI, google-genai, Deno): context7 MCP.
