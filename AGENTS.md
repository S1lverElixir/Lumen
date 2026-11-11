# AGENTS.md — Lumen: instructions for AI sessions in this repo

Lumen is a Telegram bot with a persona styled after Claude that never names the model behind it:
one FastAPI + aiogram webhook process on a Hugging Face Space (Docker, Python 3.13,
`CMD ["python", "-u", "bot.py"]`), plus a small Deno pass-through proxy for the hosts that block
Hugging Face egress. The Claude look is deliberate: when you change how the bot answers, keep that
reference's layout, its live status bubble and its plain tone.
This file holds only what an agent cannot get from the code: the owner's rules, the commands, the
traps; how any feature works is in `docs/`. Every fact below was checked against the repo and
production on 10.11.2026, so change a fact and update this file in the same commit.

## Owner and replies
- The owner does not write code; the bot was built with AI help. Reply in Russian, in plain words,
  short: what changed, why, result. Go deeper only when asked. Explain any technical term in a
  phrase. No emojis, no heavy markdown.
- Start every conversational reply with "SilverElixir," (a canary: when it disappears, the owner
  restarts the session). The chat message that delivers a report still starts with it; the
  report text itself, code, file contents and commit messages never contain it.
- The owner's task overrides this file, except Approvals and Secrets: push, merge, deploy, talking
  to the live bot and reading secrets each need confirmation of that specific action.
- Ambiguous requirement: ask one short question instead of guessing. If the answer changes little,
  take the simpler option and state the assumption you used.

## Approvals and git
- Small requested fixes: do them right away. Large changes (a new dependency, behavior change,
  restructuring, streaming or security layers): short plan first, wait for approval.
- Protected files, an approved plan first even for a one-line fix: `system_prompt.py`,
  `lumen_security.py`, `lumen_router_config.py`, `.github/workflows/*`, `Dockerfile`. The same list
  is machine-enforced as `ask` in `opencode.json`; change this file and `opencode.json` together.
- This working tree is shared: other agent sessions and the owner edit and commit in it while you
  work. Never `git switch`, `git stash`, `git restore`, `git clean`, `git add -A` or
  `git commit -a`; stage the exact paths you changed and re-check `git status` before committing.
  Commit your own files promptly: another session's broad commit absorbs your uncommitted work into
  its message, which happened to this file on 10.11.2026.
- Work on a branch `agent/<short-topic>`, never on main; the branch comes from the owner or from a
  separate `git worktree`, so never move the shared checkout yourself and ask if it sits on `main`.
  Commit there yourself, in small logical steps, one line saying what and why, in the language of
  the recent git log (Russian). The owner merges. Never push, merge to main, deploy to the Space,
  or message the live bot on your own. The hand fallback for the proxy is the owner's too: it needs
  `DENO_DEPLOY_TOKEN`, which you do not hold.

## Secrets and privacy
- Never read or print `.env` or any key value. Some free model tiers train on prompts.
- Never call the live admin endpoints: `/admin_keys` (service keys), `/export_state` (every chat
  history), `/devtrace` (user texts and answers), `/webhook_url`, `/diag`. Never paste their output
  anywhere. The daily `state-backup.yml` already calls `/export_state` on a schedule; that is the
  owner's plumbing, not a command for you.
- Reading is allowed where the platform keeps secrets out of reach. Each of these is a complete
  command: `NO_PROXY= hf spaces logs SilverElixir/Lumen`,
  `NO_PROXY= hf spaces secrets list SilverElixir/Lumen`, `gh secret list`. They return runtime logs
  and secret names; secret values are never returned by either API. The fourth form,
  `NO_PROXY= hf spaces variables list SilverElixir/Lumen`, prints ordinary variables together with
  their values, so treat that output as public configuration, not as a place to look for a key.
  Clearing `NO_PROXY` is mandatory for every `hf` call (`NO_PROXY= ` prefix in Git Bash,
  `$env:NO_PROXY=''` in PowerShell): the owner's global value contains `[::1]`, which kills any
  httpx-based tool with `InvalidURL: Invalid port` (prod 09.11.2026).
- Logs are evidence the owner and the tooling read (`hf spaces logs`, Sentry, `devtrace`), so a
  log line is a publication: never write user text, media bytes or a key value into one. Secrets
  are scrubbed on the way out (`bot.py` redacts the provider keys, Sentry scrubs again in
  `before_send`), and `devtrace.jsonl` is gitignored for the same privacy reason as the state
  export. Keep an identifier or a hash, not the content.
- Found a leaked secret? Tell the owner to rotate it. Do not rewrite git history without asking.

## Commands
- Setup, once per session: `pip install -r requirements.txt -r requirements-dev.txt`
- Gate: `pyflakes bot.py lumen_*.py system_prompt.py tests/*.py` then `pytest -q`. The suite is
  over a thousand tests and takes under a minute. CI runs these two plus the three deno proxy
  checks and `pip-audit` on every push, so the extra ones are your job locally only when you touch
  those files. One file: `pytest tests/<file>.py -v`; one test:
  `pytest tests/<file>.py::<name> -v`.
- Globs do not expand in a PowerShell shell (opencode on Windows), so there the gate is these two
  lines verbatim:

  ```powershell
  pyflakes bot.py system_prompt.py (Get-ChildItem lumen_*.py | ForEach-Object Name) (Get-ChildItem tests -Filter *.py | ForEach-Object { "tests/" + $_.Name })
  pytest -q
  ```

  In Qoder the Bash tool is Git Bash, where the Linux form above works exactly as written. Read and
  search with the file tools rather than `cat` and `grep`, and never wrap a command in `bash -lc`
  just to get globs.
- Proxy gate, run it whenever you change `proxy/`: `deno test proxy/proxy_test.ts` (offline, no
  external imports), `deno check proxy/proxy.ts`, `deno lint proxy/proxy.ts`.
- `pip-audit -r requirements.txt` is not part of the local baseline: it needs network and goes red
  on any new CVE in a pinned version. Run it when you touched `requirements.txt` or when asked.
  It gates production too: a fresh CVE in a pin you never touched makes CI red and stops the Space
  sync. Report it, never "fix" it by re-pinning; CI reruns weekly, so a quiet repo still gets
  checked.
- Do not start the bot locally; it needs live tokens and a webhook. Live checks after a deploy
  (`/diag` plus the main commands) are the owner's job, and so are the routing checks `/selftest`
  and `/stats`.
- Run the gate before changing anything (green baseline) and after each batch. If it is red before
  your changes, stop and report which checks fail; do not carry on with the task or fix unrelated
  failures unless the owner says to. A missing dev dependency is the exception: install it.
- Done means: you ran the gate and it is green, and the report is written. Never say "done" or
  "tests pass" otherwise.

## Deploy
- A green CI run on main is a deploy: `.github/workflows/sync-to-hf.yml` waits for CI, then pushes
  exactly the tested commit to the Space with a lease. A hotfix made directly on the Space makes the
  sync fail loudly instead of being overwritten. Batch work into one merge: every merged commit re-queues a Docker build, and while the build is queued the Space answers 503 and nobody gets an answer (measured 11.11.2026: three merges inside 12 minutes left the build queued for 15+ minutes).
- The proxy ships separately through `deploy-proxy.yml`, and it can miss a change silently: it
  deploys only when the diff of the commit CI actually tested touches `proxy/`. Merges and squashes
  are covered (the base is the merge base, or the whole squashed diff), but a rebase-style push of
  several commits tests only the tip, whose base is `HEAD~1` — that is how the relay rule of
  09.11.2026 stayed undeployed. After any proxy change, confirm both deploy steps finished as
  success and not skipped; if they were skipped, the owner reruns the workflow from Actions (a
  manual run always deploys and needs no token) or ships `deno deploy` by hand (recipe in
  `docs/ARCHITECTURE.md`, "Deploy paths").
- Container egress is blocked or hangs toward Telegram, TikWM and `api.cloudflare.com`: those hosts
  are reachable only through the allowlist in `proxy/proxy.ts`. Adding a provider without an
  allowlist entry times out in production while every local test passes: change the allowlist,
  deploy the proxy, then ask the owner to confirm with `/diag`. The relay itself has limits no
  local run shows: it kills an upstream at 60 s (`504`), refuses a request body over 100 MB
  (`413`) but cuts an oversized reply mid-stream, and never follows a redirect, deliberately
  (`redirect: "error"`) — so a timeout above 60 s, a large upload or a provider behind a 302
  fails only on the Space.
- A Space variable or secret is read once at import, because `bot.py` binds its configuration to
  module-level constants: a change in the Space settings only lands after the Space restarts, so
  never report that a switch "took effect" while the old process is still running. Only the owner
  sets or changes Space variables and secrets.
- What ships is not what is in the repo: `.dockerignore` keeps the tests, the docs, the proxy and
  the CI config out of the image, so a runtime path reading one of those passes tests and fails in
  production. The image rebuilds from scratch every deploy, which is why `requirements.txt` pins
  with `==` (see its header): a version bump is a decision plus a smoke test, never a side effect.

## Report
In this order, no padding: what changed and why; changed files; gate result (plus proxy gate if
run); what was not verified; what the owner must check by hand (a smoke test after a version bump
or a routing change). If nothing is unverified or risky, say so in one line.

## Where things live
- `bot.py` is the entry point and composition root (env, logging, Dispatcher, main) and imports
  hundreds of names from the `lumen_*.py` modules: a grep for a symbol hits `bot.py` first, the
  definition is in the module. `lumen_admin.py` holds the FastAPI app and the admin endpoints; the
  rest is in the `lumen_*.py` modules, whose docstring says what each one does. A name ending in
  `_flow` or `_calls` is the Telegram-touching half of the plain module it pairs with.
- A user-visible string is a key, never a literal: it lives in `lumen_lang.py` (over 300 KB of fixed
  strings in 25 languages, so grep a narrow pattern instead of reading it) and is read through
  `_t(chat_id, "key")`; buttons are assembled in `lumen_commands.py`. A new key must be filled in
  all 25 language packs, or `test_lang_table_covers_all_keys_in_all_languages` in
  `tests/test_bot_commands.py` fails.
- The bot's Telegram profile (name, the "About" one-liner, the long description) is set by code, not
  by BotFather: `_register_bot_profile` in `bot.py` sends the `profile_*` keys of `lumen_lang.py` for
  the default locale plus `PROFILE_LANGS` on every startup, so a hand edit in BotFather is undone by
  the next deploy. 64/120/500 characters are Telegram's caps, checked by
  `test_profile_strings_fit_telegram_limits`.

## Providers, in one screen
The backbone since 05.11.2026 is Munito AI (an OpenAI-compatible endpoint serving open models), and
Gemini is the unconditional reserve on every route where Munito answers.
- Text and image input: Munito (`_MUNITO_ORDER`) is the head. Reading a site by
  URL, YouTube, video, audio input and TTS: Gemini only. Voice notes are transcribed by Groq
  Whisper first, the only thing Groq is still used for.
- `/draw` is a different provider: Cloudflare Workers AI (`@cf/black-forest-labs/flux-2-klein-4b`)
  does both generation and edits, Pollinations (`sana`) is the reserve and only generates; the
  chain falls back by provider, not by model. A moderation refusal is terminal for the chain and
  still costs the person their personal slot.
- Freshness: no Munito model has web search, so every provider also gets an external snippet block
  built by `lumen_search.py` from three layers — exact fact APIs (Wikipedia, crypto, FX, weather), dated news feeds and the general web (DuckDuckGo lite then its HTML endpoint, Bing RSS) — after the user's wording is cleaned into search forms; an optional keyed provider (Tavily/Brave/Exa) replaces the scraping layer. Without a key there is no reliable general engine: measured 11.11.2026, DuckDuckGo lite returned an anomaly page 8/8 and Brave/Ecosia/Qwant/Yandex/Startpage/SearxNG are all blocked. The order and merge rules are in
  `docs/ARCHITECTURE.md`. A model unsure about a post-cutoff fact may instead answer with the bare
  marker `[[SEARCH]]`: it is intercepted before anything is shown and buys one searched retry, so
  that exception must never reach the model-failure marks (mechanics in `docs/ARCHITECTURE.md`).
- Limits are derived, not picked: a daily cap sits under the provider's real free-tier budget with
  headroom, and per-user caps follow the existing family (one for messages, one per heavy feature).
  The global cap is what protects a shared provider, so a reflexively small number is not safety.
  Put the arithmetic in the plan, and say in it whether the number is an env variable at all:
  `docs/ENVIRONMENT.md` records the reasoning and lists the limits that are hardcoded.
- User-facing text must never name a provider or a model, including in an error message. The leak
  filter in `lumen_security.py` and the persona in `system_prompt.py` are the source of truth.

## Gotchas
- A model leaves routing only with dated evidence (production log errors or provider docs), written
  as a comment next to the chain it came out of: `_MUNITO_ORDER` or the `GEMINI_*_CHAIN` lists in
  `lumen_router_config.py`. Transient failures are not exclusions: `503`/`429` set a cooldown stored
  inside the quota state, so it outlives a restart only when Upstash is configured, and repeated
  empty answers set an in-memory quarantine that a restart always clears. Never remove a model on a
  guess, and remember that editing those lists needs an approved plan.
- There is no model-health registry: `_OR_MODEL_HEALTH` went away with OpenRouter on 05.11.2026 and
  was not replaced, so do not look for one or build it again.
- In that same file `GEMINI_DEFAULT_CHAIN`, `GEMINI_LINK_CHAIN` and `GEMINI_LINK_SEARCH_CHAIN` are
  copies, not aliases, of the heavy and search chains (audit 26.09.2026): editing one list does not
  move the others, so check which copy a route actually reads.
- Streaming has no automated test of a real transport: every client is mocked. Munito SSE from the
  deployed container is proven in production (Space logs 10.11.2026: answers received via streaming
  on the Munito head); Gemini SSE has only ever been exercised by hand through the smoke bot, and no
  automated test covers a failover. Before touching streaming code, follow the manual smoke
  checklist in `docs/ARCHITECTURE.md` and put in the report which points the owner has to run
  after the deploy: the bot is not yours to drive, so you never run those checks yourself.
- `python bot.py` makes the module `__main__` while tests import it as `bot`; the alias at the top
  of `bot.py` (`sys.modules.setdefault("bot", ...)`) keeps them one module, and every `import bot`
  in `lumen_*.py` is function-level on purpose. Hoisting one, or dropping the alias, gives two
  instances with separate state and a permanent 503 with the whole suite green; the comment above
  that alias in `bot.py` records the incident.
- Any new path that touches a chat runs under `_run_under_chat_lock` (`lumen_message_core.py`): only
  it enforces the waiter cap (`_MAX_CHAT_LOCK_WAITERS`) and the honest busy reply, and it supplies
  the message deadline (`MESSAGE_TOTAL_BUDGET_SEC`) and the `chat=` log label when the caller did
  not (ordinary messages get both from `handle_message` in `bot.py` before the lock). A handler may
  be registered outside the lock and still take it around the work it starts (`/draw` did not, and
  two people in one group each left a live button over one photo; fixed 10.11.2026).
- After a failed startup load `_state_load_failed` (`lumen_chat_state.py`) suppresses index and
  quota writes, so an Upstash outage cannot overwrite real history with empty in-memory state. Any
  new writer must honor that flag (audit A5-2, 28.10.2026).
- `unittest.mock.patch` only replaces the name in the namespace that reads it: patch
  `lumen_router_config.X`, not `bot.X`.
- With `core.autocrlf=true` and no `.gitattributes`, almost the whole working tree is CRLF
  (two tracked files are not), so multi-line edits often fail to match because of the CR
  character. Edit single lines, and never reflow a whole file to LF as a side effect. Source
  files are UTF-8 with Cyrillic, and `col_offset` in an AST is a byte offset: a script that
  patches by offset must work on bytes.
  A patcher that splits on CRLF and rejoins on CRLF corrupts a file that an earlier
  patch left with LF lines: the whole file then shows as changed. When that
  happens, rebuild the file from `git show HEAD:<path>` and re-apply the one
  intended insert; never fix it by converting endings.

## Code style
- Targeted edits on existing files. Never rewrite a file from scratch unless a split was approved.
- Comments in Russian, except structural headers carrying identifiers or commands. Say why (reason,
  incident date, tradeoff), 2-3 lines max, never restate the code. Comment only where something is
  non-obvious: magic numbers, regexes, workarounds, ordering constraints, surprising behavior.
- Keep dated regression markers short: `(prod ДД.ММ.ГГГГ: what happened)`, or just the date when
  the sentence already says what happened; `(audit ...)`, `(review ...)` the same way. Long
  write-ups belong in `docs/`.
- Banned in code, comments and docs: paraphrasing the code, filler ("Важно", "Здесь мы",
  "Следует отметить"), emojis, dash spam, commented-out code. A module docstring is one line saying
  what the module does, with no extraction history ("вынесено из bot.py").
- Before writing new code, check whether the repo, the stdlib or an installed dependency already
  does it. New dependencies need approval.

## Tests
- State before adding a test: what behavior it protects, which real regression would make it fail,
  why an existing test does not catch it. No answer means no test.
- There is no pytest-asyncio, and pytest 9 fails an `async def test_*` outright ("async def
  functions are not natively supported"). Every async path is driven from a plain `def test_*` that
  calls `asyncio.run(scenario())`: follow the shape the whole suite already uses.
- `tests/conftest.py` stubs `BOT_TOKEN`, `GEMINI_API_KEY`, `BOT_LOG_PATH` and blanks the other
  provider and experiment variables (Munito, Cloudflare, Groq, OpenRouter, Sentry), so a local
  `.env` cannot send a test to a live API. Because
  those keys stay empty, tests only see the Gemini and Pollinations branches: to exercise Munito or
  Cloudflare a fixture must set the key itself — a fake value for one test, never a real key in a
  test or a fixture. When a module starts reading a new key at import time, add it to that list.
- Waits go through a module-level seam read at call time, and `tests/conftest.py` replaces two of
  them for the whole suite: `bot._typing_sleep` and `lumen_tiktok._sleep`, each read from its own
  module.
  A direct `asyncio.sleep` therefore slows every test in the run. Network has the same seam shape
  (`bot._get_http_session`) but is patched per test, not suite-wide, so new code that reaches out
  is tested by patching that name in the module that reads it.
- Reuse the shared fakes in `tests/bot_test_helpers.py` instead of writing new ones, and extend an
  existing parametrized test rather than add a near-duplicate.
- A bug-fix test must fail on the old code and pass on the fixed one; run it both ways.
- Do not write: tests without assertions, a value compared with itself, a mock that reimplements
  the behavior being asserted, greps over source text.

## Docs, mirrors and tools
The same facts live in several places and they drift. A change to behavior updates them in the same
commit: `docs/ARCHITECTURE.md` (pipelines, routing, the streaming checklist, deploy paths),
`docs/ENVIRONMENT.md` (variables and hardcoded limits), `README.md` and `README.ru.md` (two
languages of one document, so a user-facing change edits both), this file, and `opencode.json` when
a permission changed. A generated RepoWiki may sit in the gitignored `.qoder/` directory: it is
not committed, it drifts, and `docs/` stays the truth.
- `eval/` holds the offline probes and the persona harness, run by hand from main through
  `persona-eval.yml`; method and results are in `docs/PROMPT_EVAL.md`, and its dated measurements
  are what the evidence rule in Gotchas expects. `eval/harness_eval.py` measures this file itself:
  it replays real fix-commits through an agent and judges by the repo gate. Eval data is part of
  the suite: `tests/test_eval_persona.py` fails when the question ids in
  `eval/persona_questions.json` stop being exactly 1..25 or a question loses its expectation.
- Sentry: organization `silverelixir`. `find_organizations`, then `search_issues` with
  `organizationSlug=silverelixir`, then `get_sentry_resource` with `resourceType=issue` for one
  issue. Library docs (aiogram, FastAPI, google-genai, Deno): context7, `resolve-library-id` then
  `query-docs` — configured as a user-level connector in Qoder (the app has no per-project scope
  for custom connectors) and as an MCP server in `opencode.json`. A connector added mid-session
  appears only in a new chat; if the tools are missing here, say so instead of reciting
  documentation from memory.

## About this file
- Keep here only what an agent cannot learn from the code in a minute of searching: explanations of
  behavior go to `docs/`, mechanics into comments.
- Add a rule once the same mistake has repeated, drop a rule that no longer changes behavior, and
  re-run every command you edited here before committing it.
- Keep it a single file. Qoder Desktop loads the root `AGENTS.md`; nested per-directory `AGENTS.md`
  and `.qoder/rules` activation triggers are documented for Qoder CLI only, so a rule moved out of
  here stops being seen in this app. Verified against docs.qoder.com on 10.11.2026.
