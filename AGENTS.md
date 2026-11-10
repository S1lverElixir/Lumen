# AGENTS.md — Lumen: instructions for AI sessions in this repo

Lumen is a Telegram bot with a persona styled after Claude that never names the model behind it:
one FastAPI + aiogram webhook process on a Hugging Face Space (Docker, Python 3.13,
`CMD ["python", "-u", "bot.py"]`), plus a small Deno pass-through proxy for the hosts that block
Hugging Face egress. The Claude look is a goal, not a coincidence: keep the answer layout, the live
status bubble and the plain tone in anything a user reads.
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
- Work on a branch `agent/<short-topic>`, never on main. Commit there yourself, small logical
  steps, one line saying what and why, in the language of the recent git log (Russian). The owner
  merges. Never push, merge to main, deploy to the Space, or message the live bot on your own.

## Secrets and privacy
- Never read or print `.env` or any key value. Some free model tiers train on prompts.
- Never call the live admin endpoints: `/admin_keys` (service keys), `/export_state` (every chat
  history), `/devtrace` (user texts and answers), `/webhook_url`, `/diag`. Never paste their output
  anywhere. The daily `state-backup.yml` already calls `/export_state` on a schedule; that is the
  owner's plumbing, not a command for you.
- Reading is allowed where the platform hides values: `hf spaces logs SilverElixir/Lumen`,
  `hf spaces variables list SilverElixir/Lumen` and `gh secret list` give logs and names only,
  never values. Prefix `hf` with `NO_PROXY` cleared for that one command (`NO_PROXY= hf ...` in Git
  Bash, `$env:NO_PROXY=''` in PowerShell): the owner's global value contains `[::1]`, which kills
  any httpx-based tool with `InvalidURL: Invalid port` (found 09.11.2026).
- Found a leaked secret? Tell the owner to rotate it. Do not rewrite git history without asking.

## Commands
- Setup, once per session: `pip install -r requirements.txt -r requirements-dev.txt`
- Gate: `pyflakes bot.py lumen_*.py system_prompt.py tests/*.py` then `pytest -q`. The suite is
  over a thousand tests and takes under a minute. CI runs these two plus the three deno proxy
  checks and `pip-audit` on every push, so the extra ones are your job locally only when you touch
  those files.
- Globs do not expand in PowerShell (opencode sessions), so there run
  `pyflakes bot.py system_prompt.py (Get-ChildItem lumen_*.py | ForEach-Object Name) (Get-ChildItem tests -Filter *.py | ForEach-Object { "tests/" + $_.Name })`
  then `pytest -q`, and read or search with the file tools rather than `cat` and `grep`. Never wrap
  a command in `bash -lc` just to get globs. In Qoder the Bash tool is Git Bash, where the first
  form works as written.
- One file: `pytest tests/<file>.py -v`. One test: `pytest tests/<file>.py::<name> -v`.
- Proxy gate, run it whenever you change `proxy/`: `deno test proxy/proxy_test.ts` (offline, no
  external imports), `deno check proxy/proxy.ts`, `deno lint proxy/proxy.ts`.
- `pip-audit -r requirements.txt` is not part of the local baseline: it needs network and goes red
  on any new CVE in a pinned version. Run it when you touched `requirements.txt` or when asked.
  It also blocks deployment: a fresh CVE in an untouched pin makes CI red and stops the Space sync
  even when your code is clean. Report it, never "fix" it by re-pinning. CI reruns on a weekly
  schedule too, so a quiet repo still gets checked.
- Do not start the bot locally; it needs live tokens and a webhook. Live checks after a deploy
  (`/diag` plus the main commands) are the owner's job.
- Run the gate before changing anything (green baseline) and after each batch. If it is red before
  your changes, stop and report which checks fail; do not carry on with the task or fix unrelated
  failures unless the owner says to. A missing dev dependency is the exception: install it.
- Done means: you ran the gate and it is green, and the report is written. Never say "done" or
  "tests pass" otherwise.

## Deploy
- A green CI run on main is a deploy: `.github/workflows/sync-to-hf.yml` waits for CI, then pushes
  exactly the tested commit to the Space with a lease. A hotfix made directly on the Space makes the
  sync fail loudly instead of being overwritten.
- The proxy ships separately through `deploy-proxy.yml`, and it can miss a change silently: it
  deploys only when the diff of the commit CI actually tested touches `proxy/`, so a proxy change
  pushed together with other commits never deploys on its own (prod 09.11.2026). After any proxy
  change, confirm that both deploy steps of that run finished as success and not skipped; otherwise
  deploy by hand (`docs/ARCHITECTURE.md`, section "Deploy paths").
- Container egress is blocked or hangs toward Telegram, TikWM and `api.cloudflare.com`: those hosts
  are reachable only through the allowlist in `proxy/proxy.ts`. Adding a provider without an
  allowlist entry times out in production while every local test passes: change the allowlist,
  deploy the proxy, then ask the owner to confirm with `/diag`. The relay itself has limits no
  local run shows: it kills an upstream at 60 s (`504`), refuses bodies over 100 MB (`413`) and
  never follows a redirect, deliberately (`redirect: "error"`) — so a timeout above 60 s, a large
  upload or a provider behind a 302 fails only on the Space.
- What ships is not what is in the repo: `.dockerignore` keeps `tests/`, `docs/`, `proxy/` and
  `.github/` out of the image, so a runtime path that reads one of those passes the tests and fails
  in production. The image is rebuilt from scratch on every deploy, which is why
  `requirements.txt` pins with `==` (see its header): bumping a version is a deliberate decision
  followed by a manual smoke test, never a side effect.

## Report
Short, in this order: what changed and why; changed files; gate result (plus proxy gate if run);
what was not verified; what the owner must check by hand (a smoke test after a version bump or a
routing change). If nothing is unverified or risky, say so in one phrase instead of padding.

## Where things live
- `bot.py` is the entry point and composition root (env, logging, Dispatcher, main) and re-exports
  over 250 names from the `lumen_*.py` modules: a grep for a symbol hits `bot.py` first, the
  definition is in the module. `lumen_admin.py` holds the FastAPI app and admin endpoints; the rest
  is in the 30 `lumen_*.py` files, each opening with a one-line docstring. A name ending in `_flow`
  or `_calls` is the Telegram-touching half of the plain module next to it.
- `lumen_lang.py` is over 300 KB of fixed UI strings in 25 languages. Never read it whole; grep a
  narrow pattern. Any new user-visible string must be added in all 25 languages or the
  completeness test fails.

## Providers, in one screen
The backbone since 05.11.2026 is Munito AI (an OpenAI-compatible endpoint serving open models) with
Gemini as the unconditional automatic reserve.
- Text and image input: Munito (`_MUNITO_ORDER`) first, Gemini as the reserve. Reading a site by
  URL, YouTube, video, audio input and TTS: Gemini only. Voice notes are transcribed by Groq
  Whisper first, the only thing Groq is still used for.
- `/draw` is a different provider: Cloudflare Workers AI (`@cf/black-forest-labs/flux-2-klein-4b`)
  does both generation and edits, Pollinations (`sana`) is the reserve and only generates. Fallback
  is by provider, not by model. A moderation refusal is terminal for the chain and still costs the
  person their personal slot.
- Freshness: no Munito model has web search, so every provider also gets an external snippet block
  built from DuckDuckGo, Bing and Google News; the order and merge rules are in
  `docs/ARCHITECTURE.md`.
- User-facing text must never name a provider or a model, including in an error message. The leak
  filter in `lumen_security.py` and the persona in `system_prompt.py` are the source of truth.

## Gotchas
- A model leaves routing only with dated evidence (production log errors or provider docs), written
  as a comment next to the chain it came out of: `_MUNITO_ORDER` or the `GEMINI_*_CHAIN` lists in
  `lumen_router_config.py`. Transient failures are not exclusions: `503`/`429` set a cooldown stored
  inside the quota state, so it outlives a restart only when Upstash is configured, and repeated
  empty answers set an in-memory quarantine that a restart always clears.
  The old `_OR_MODEL_HEALTH` registry went away with OpenRouter on 05.11.2026 and has no successor:
  do not look for it or rebuild it. Never remove a model on a guess, and remember that file is
  protected: the change goes through an approved plan.
- In that same file `GEMINI_DEFAULT_CHAIN`, `GEMINI_LINK_CHAIN` and `GEMINI_LINK_SEARCH_CHAIN` are
  copies, not aliases, of the heavy and search chains (аудит 26.09.2026): editing one list does not
  move the others, so check which copy a route actually reads.
- Streaming has no automated test of a real transport: every client is mocked. Munito SSE from the
  deployed container is proven in production (Space logs 10.11.2026: answers received via streaming
  on the Munito head); Gemini SSE has only ever been exercised by hand through the smoke bot, and
  no failover path is covered at all. Before touching streaming code, follow the manual smoke
  checklist in `docs/ARCHITECTURE.md` and list in the report which points the owner must run.
- Running `python bot.py` makes the module `__main__` while tests import it as `bot`; the alias at
  the top of `bot.py` (`sys.modules.setdefault("bot", ...)`) keeps them one module, and every
  `import bot` inside `lumen_*.py` is function-level on purpose. Hoisting one to the top of a file,
  or dropping the alias, gives two instances with separate state and a permanent 503 while the
  whole suite stays green (прод сентябрь 2026).
- Any new path that touches a chat runs under `_run_under_chat_lock` (`lumen_message_core.py`):
  outside it a request skips the message budget, the waiter cap (`_MAX_CHAT_LOCK_WAITERS`) and the
  busy reply, and `/draw` outside the lock left two live buttons over one photo (review 10.11.2026).
- After a failed startup load `_state_load_failed` (`lumen_chat_state.py`) suppresses index and
  quota writes, so an Upstash outage cannot overwrite real history with empty in-memory state. Any
  new writer must honor that flag (audit A5-2, 28.10.2026).
- `unittest.mock.patch` only replaces the name in the namespace that reads it: patch
  `lumen_router_config.X`, not `bot.X`.
- The working tree is CRLF (`core.autocrlf=true`, no `.gitattributes`), so multi-line edits often
  fail to match because of `\r`. Edit single lines, and never reflow a whole file to LF as a side
  effect. Source files are UTF-8 with Cyrillic, and `col_offset` in an AST is a byte offset: a
  script that patches by offset must work on bytes.

## Code style
- Targeted edits on existing files. Never rewrite a file from scratch unless a split was approved.
- Comments in Russian, except structural headers carrying identifiers or commands. Say why (reason,
  incident date, tradeoff), 2-3 lines max, never restate the code. Comment only where something is
  non-obvious: magic numbers, regexes, workarounds, ordering constraints, surprising behavior.
- Keep dated regression markers short: `(prod ДД.ММ.ГГГГ: ...)`, `(audit ...)`, `(review ...)`.
  Long write-ups belong in `docs/`.
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
- `tests/conftest.py` stubs `BOT_TOKEN`, `GEMINI_API_KEY`, `BOT_LOG_PATH` and blanks ten more
  provider and experiment variables, so a local `.env` cannot send a test to a live API. Because
  those keys stay empty, tests only see the Gemini and Pollinations branches: to exercise Munito or
  Cloudflare a fixture must set the key itself — a fake value for one test, never a real key in a
  test or a fixture. When a module starts reading a new key at import time, add it to that list.
- Sleeps and network calls go through a module-level seam read through `bot.` at call time
  (`_typing_sleep`, `_get_http_session`, `lumen_tiktok._sleep`), which `conftest.py` replaces for
  the whole suite: a direct `asyncio.sleep` slows every test, and a seam imported by value is never
  replaced, so the test passes without waiting or calling anything.
- Reuse the shared fakes in `tests/bot_test_helpers.py` instead of writing new ones, and extend an
  existing parametrized test rather than add a near-duplicate.
- A bug-fix test must fail on the old code and pass on the fixed one; run it both ways.
- Do not write: tests without assertions, a value compared with itself, a mock that reimplements
  the behavior being asserted, greps over source text.

## Docs, mirrors and tools
The same facts live in several places and they drift. A change to behavior updates them in the same
commit: `docs/ARCHITECTURE.md` (pipelines, routing, the streaming checklist, deploy paths),
`docs/ENVIRONMENT.md` (variables and hardcoded limits), `README.md` and `README.ru.md`
(translations of one text: both, or neither), this file, and `opencode.json` when a permission
changed. A generated RepoWiki may sit in the gitignored `.qoder/` directory: it is not committed,
it drifts, and `docs/` stays the truth.
- `eval/` holds the offline probes and the persona harness, run by hand from main through
  `persona-eval.yml`; method and results are in `docs/PROMPT_EVAL.md`, and its dated measurements
  are what the evidence rule in Gotchas expects. `eval/harness_eval.py` measures this file itself:
  it replays real fix-commits through an agent and judges by the repo gate. Eval data is part of
  the suite: `tests/test_eval_persona.py` fails when the question ids in
  `eval/persona_questions.json` stop being exactly 1..25 or a question loses its expectation.
- Sentry: organization `silverelixir`. `find_organizations`, then `search_issues` with
  `organizationSlug=silverelixir`, then `get_sentry_resource` with `resourceType=issue` for one
  issue. Library docs (aiogram, FastAPI, google-genai, Deno): context7 MCP. Both are MCP servers
  configured in `opencode.json`, so they exist only in a session that loaded it; if the tools are
  not there, say so instead of guessing at logs.
