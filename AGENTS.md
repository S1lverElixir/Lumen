# AGENTS.md — Lumen: instructions for AI sessions in this repo

Lumen is a Telegram bot with a persona styled after Claude that never names the model behind it:
one FastAPI + aiogram webhook process on a Hugging Face Space (Docker, Python 3.13,
`CMD ["python", "-u", "bot.py"]`), plus a small Deno pass-through proxy for the hosts that block
Hugging Face egress. The Claude look is a goal, not a coincidence: keep the answer layout, the live
status bubble and the plain tone in anything a user reads.
This file holds what an agent cannot get from the code: the owner's rules, the commands, the traps.
How any feature works is in `docs/`. Every fact below was checked against the repo and production
on 10.11.2026; a commit that changes a fact here updates this file in the same commit.

## Owner and replies
- The owner does not write code; the bot was built with AI help. Reply in Russian, in plain words,
  short: what changed, why, result. Go deeper only when asked. Explain any technical term in a
  phrase. No emojis, no heavy markdown.
- Start every conversational reply with "SilverElixir," (a canary: when it disappears, the owner
  restarts the session). Never put it in code, file contents, commit messages, or reports.
- The owner's task overrides this file, except Approvals and Secrets: push, merge, deploy, talking
  to the live bot and reading secrets each need confirmation of that specific action.
- Ambiguous requirement: ask one short question instead of guessing. If the answer changes little,
  take the simpler option and state the assumption you used.

## Approvals and git
- Small requested fixes: do them right away. Large changes (a new dependency, behavior change,
  restructuring, streaming or security layers): short plan first, wait for approval.
- Protected files, approval needed even for a one-line fix: `system_prompt.py`, `lumen_security.py`,
  `lumen_router_config.py`, `.github/workflows/*`, `Dockerfile`. The same list is machine-enforced
  as `ask` in `opencode.json`; change this file and `opencode.json` together.
- Work on a branch `agent/<short-topic>`, never on main. Commit there yourself, small logical
  steps, one line saying what and why, in the language of the recent git log (Russian). The owner
  merges. Never push, merge to main, deploy to the Space, or message the live bot on your own.

## Secrets and privacy
- Never read or print `.env` or any key value. Some free model tiers train on prompts.
- Never call the live admin endpoints: `/admin_keys` (service keys), `/export_state` (every chat
  history), `/devtrace` (user texts and answers), `/webhook_url`, `/diag`. Never paste their output
  anywhere. The daily `state-backup.yml` already calls `/export_state` on a schedule; that is the
  owner's plumbing, not a command for you.
- Reading is allowed where the platform hides values: `NO_PROXY= hf spaces logs SilverElixir/Lumen`
  and `NO_PROXY= hf spaces variables list SilverElixir/Lumen` give runtime logs and secret names,
  `gh secret list` gives repo secret names. Values are never returned by either API. Clearing
  `NO_PROXY` is mandatory: the owner's global value contains `[::1]`, which kills any httpx-based
  tool with `InvalidURL: Invalid port` (found 09.11.2026).
- Found a leaked secret? Tell the owner to rotate it. Do not rewrite git history without asking.

## Commands
- Setup, once per session: `pip install -r requirements.txt -r requirements-dev.txt`
- Gate: `pyflakes bot.py lumen_*.py system_prompt.py tests/*.py` then `pytest -q`. About 1200 tests,
  around 30 seconds. This is what CI runs; CI also always runs the three deno proxy checks and
  `pip-audit`, whether or not you touched them.
- Globs do not expand in PowerShell (opencode sessions), so there run
  `pyflakes bot.py system_prompt.py (Get-ChildItem lumen_*.py | ForEach-Object Name) (Get-ChildItem tests -Filter *.py | ForEach-Object { "tests/" + $_.Name })`
  then `pytest -q`, and read or search with the file tools instead of `cat` and `grep`. In Qoder
  the Bash tool is Git Bash, where the first form works as written.
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
  your changes, stop and report which checks fail. A missing dev dependency is the exception.
- Done means: you ran the gate and it is green, and the report is written. Never say "done" or
  "tests pass" otherwise.

## Report
Short, in this order: what changed and why; changed files; gate result (plus proxy gate if run);
what was not verified; what the owner must check by hand (a smoke test after a version bump or a
routing change). If nothing is unverified or risky, say so in one phrase instead of padding.

## Where things live
- `bot.py` is the entry point and composition root (env, logging, Dispatcher, main), and it
  re-exports about 300 names from the `lumen_*.py` modules: a grep for any symbol hits `bot.py`
  first, but the definition is in the module. `lumen_admin.py` holds the FastAPI app and the admin
  endpoints; the other domain logic is in the 31 `lumen_*.py` files, each opening with a one-line
  docstring. A name ending in `_flow` or `_calls` is the Telegram-touching half of the plain module
  next to it, which holds the mechanics.
- `lumen_lang.py` is over 300 KB of fixed UI strings in 25 languages. Never read it whole; grep a
  narrow pattern. Any new user-visible string must be added in all 25 languages or the
  completeness test fails.

## Providers, in one screen
- Text and image input: Munito AI (`_MUNITO_ORDER`) with Google Gemini appended as an unconditional
  reserve. Reading a site by URL, YouTube, video, audio input and TTS: Gemini only. Voice notes are
  transcribed by Groq Whisper first, the only thing Groq is still used for.
- `/draw` is a different provider: Cloudflare Workers AI (`@cf/black-forest-labs/flux-2-klein-4b`)
  does both generation and edits, Pollinations (`sana`) is the reserve and only generates. Fallback
  is by provider, not by model. A moderation refusal is terminal for the chain and still costs the
  person their personal slot.
- Freshness: every provider also gets an external snippet block from DuckDuckGo, Bing and Google
  News (news queries merge DuckDuckGo with Google News; otherwise DuckDuckGo, then Bing, then
  News), because no Munito model has web search.
- User-facing text must never name a provider or a model, including in an error message. The leak
  filter in `lumen_security.py` and the persona in `system_prompt.py` are the source of truth.

## Gotchas
- A green CI run on main is a deploy: `sync-to-hf.yml` waits for CI, then pushes exactly the tested
  commit to the Space with a lease. A hotfix made directly on the Space makes the sync fail loudly
  instead of being overwritten.
- The proxy has its own deploy (`deploy-proxy.yml`), and it can miss a change silently: it deploys
  only when the diff of the commit CI tested touches `proxy/`. On 09.11.2026 a `proxy/proxy.ts`
  change reached main, a second push cancelled its CI run, the deploy step then saw no proxy diff,
  and both Deno apps kept the old revision. After any proxy change, check that the deploy steps of
  that run really executed; if they were skipped, deploy by hand (recipe: `docs/ARCHITECTURE.md`,
  section "Deploy paths").
- Container egress is blocked or hangs toward every external host: Telegram Bot API, TikWM,
  `api.cloudflare.com`. They are reachable only through the allowlist in `proxy/proxy.ts`. Adding a
  provider whose host is not on that list produces timeouts in production that no local test can
  reproduce: change the allowlist, deploy the proxy, then confirm with `/diag`.
- What ships is not what is in the repo: `.dockerignore` keeps `tests/`, `docs/`, `proxy/`,
  `AGENTS.md` and `.github/` out of the image, so a runtime path that reads one of those works in
  the tests and fails in production. Versions in `requirements.txt` are pinned for the same reason
  (the Space rebuilds the image from scratch; see its header): bumping one is a deliberate decision
  followed by a manual smoke test, never a side effect.
- A model leaves routing only with dated evidence (production log errors or provider docs), written
  as a comment next to the chain it came out of: `_MUNITO_ORDER` or the `GEMINI_*_CHAIN` lists in
  `lumen_router_config.py`. Transient failures are not exclusions: `503`/`429` set a cooldown that
  survives a restart, repeated empty answers set an in-memory quarantine that a restart clears.
  Never remove a model on a guess.
- In that same file `GEMINI_DEFAULT_CHAIN`, `GEMINI_LINK_CHAIN` and `GEMINI_LINK_SEARCH_CHAIN` are
  copies, not aliases, of the heavy and search chains (аудит 26.09.2026): editing one list does not
  move the others, so check which copy a route actually reads.
- Streaming has no real transport test: every automated test mocks the client. Munito SSE from the
  deployed container is now proven in production (Space logs 10.11.2026: answers received via
  streaming on the Munito head), but nothing tests Gemini SSE or a failover. Before touching
  streaming code, follow the manual smoke checklist in `docs/ARCHITECTURE.md` and list in the
  report which points the owner must run by hand.
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
- There is no pytest-asyncio: tests are plain `def test_*` that call `asyncio.run(scenario())`
  inside. An `async def test_*` is collected, never awaited, and passes without testing anything.
- `tests/conftest.py` stubs `BOT_TOKEN`, `GEMINI_API_KEY`, `BOT_LOG_PATH` and blanks ten more
  provider and experiment variables, so a local `.env` cannot send a test to a live API. Because
  those keys stay empty, tests only see the Gemini and Pollinations branches: to exercise Munito or
  Cloudflare a fixture must set the key itself. When a module starts reading a new key at import
  time, add it to that list.
- Reuse the shared fakes in `tests/bot_test_helpers.py` instead of writing new ones, and extend an
  existing parametrized test rather than add a near-duplicate.
- A bug-fix test must fail on the old code and pass on the fixed one; run it both ways.
- Do not write: tests without assertions, a value compared with itself, a mock that reimplements
  the behavior being asserted, greps over source text.

## Docs and mirrors
The same facts live in several places and they drift. A change to behavior updates them in the same
commit: `docs/ARCHITECTURE.md` (pipelines, routing, the streaming smoke checklist, deploy paths),
`docs/ENVIRONMENT.md` (every environment variable and the hardcoded limits), `README.md` and
`README.ru.md` (translations of one text: both, or neither), this file, and `opencode.json` when a
permission changed. A generated RepoWiki may sit in the gitignored `.qoder/` directory: it is not
committed, it drifts, and `docs/` stays the truth.

## Evals and tools
- `eval/` holds the offline probes and the persona harness, run by hand from main through
  `persona-eval.yml`; method and results are in `docs/PROMPT_EVAL.md`, and its dated measurements
  are what the evidence rule above expects. `eval/harness_eval.py` measures this file itself: it
  replays real fix-commits through an agent with a candidate `AGENTS.md` and judges by the repo
  gate, so a longer file has to earn its length.
- Sentry: organization `silverelixir`; `find_organizations`, then `search_issues` with
  `organizationSlug=silverelixir`, then `get_sentry_resource` with `resourceType=issue` for one
  issue. Library docs (aiogram, FastAPI, google-genai, Deno): context7 MCP. Both servers come from
  `opencode.json`.
