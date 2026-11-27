# Lumen Engineering Audit

## Status

The repository inventory and R1-R11 independent reviews are complete, including the coverage-gap follow-up. Remediation batches I1-I30 are closed with independent review; I25 required seven review rounds and I26 four. I30 documentation/source-language corrections and all six post-I30 findings are fixed with `fixed_verified` re-reviews; all earlier findings are fixed or explicitly deferred with a reason. The local eval harness retains one accepted P1 host-access limitation behind a fail-closed acknowledgement; it is not part of production bot runtime.

The current inventory contains 111 changed first-party files and 5,731 elements, plus one deleted tracked file (`README.ru.md`). The earlier whole-repository review cycle closed its code findings with `fixed_verified`; the post-I30 whole-project re-review also returned `fixed_verified`. All available local gates are green. Live Telegram, provider, Docker-build and production-restore behavior remain outside local verification.

## Initial state

- Revision: `4b5efc7665d3eed08e4cf11e193efd814762d355`
- Branch: `agent/engineering-audit`
- Worktree: `C:\Users\egork\Documents\Default Project\Lumen-engineering-audit`
- Runtime: Python 3.13.17, pytest 9.2.0, pyflakes 4.1.0, Deno 2.9.8
- Architecture: one FastAPI and aiogram webhook process, a Deno pass-through proxy, Hugging Face Docker deployment, GitHub Actions CI and deployment.

## Baseline verification

| Check | Result |
|---|---|
| `pyflakes bot.py lumen_*.py system_prompt.py tests/*.py` | passed |
| `pytest -q` | 2005 passed in 42.43s |
| `deno test proxy/proxy_test.ts` | 45 passed |
| `deno check proxy/proxy.ts` | passed |
| `deno lint proxy/proxy.ts` | passed |
| `python -m pip check` | passed |
| `git diff --check` | passed |

The baseline pytest process emitted a shutdown logging error from an unclosed `aiohttp.ClientSession`. The suite exited successfully; this is a test-isolation lead, not a baseline failure.

## Inventory coverage

| Category | Count |
|---|---:|
| Tracked files | 123 |
| Inventoried files | 123 |
| Python modules | 83 |
| Classes | 230 |
| Functions and methods | 2272 |
| Async functions and methods | 1196 |
| TypeScript functions | 45 |
| Async TypeScript functions | 10 |
| Deno test declarations | 37 |
| Python test functions | 1415 |
| Total code elements | 4092 |

The machine-readable ledger is `audit/engineering-audit.jsonl`; its reproducible validator is `audit/validate_inventory.py`. Automatic AST enumeration is inventory evidence only; review status comes from full implementation and call-site review. Modified files retain baseline inventory hashes for traceability and have a separate current snapshot (111 files, 5,731 elements) regenerated across every first-party working-tree change, including `lumen_admin.py`, `lumen_state_storage.py`, the workflow YAMLs, the display/streaming/agent paths, eval tooling and their tests.

### Current inventory status

| Status | Files and elements |
|---|---:|
| `NOT_REVIEWED` | 0 |
| `REVIEWED` | 0 |
| `IMPROVEMENT_REQUIRED` | 0 |
| `REFACTORED` | 0 |
| `VERIFIED` | 5842 |
| `ACCEPTED_AS_IS` | 0 |
| `DEFERRED_WITH_REASON` | 0 |

No current file or element remains `NOT_REVIEWED` or `IMPROVEMENT_REQUIRED`. The former TikTok media-validation gaps were closed in I29 and independently verified.

### Retained 15-file working-tree snapshot status (pre-final-edits)

| Status | Files and elements |
|---|---:|
| `NOT_REVIEWED` | 0 |
| `REVIEWED` | 0 |
| `IMPROVEMENT_REQUIRED` | 38 |
| `REFACTORED` | 0 |
| `VERIFIED` | 2019 |
| `ACCEPTED_AS_IS` | 0 |
| `DEFERRED_WITH_REASON` | 0 |

## Exclusions

- `.git`, virtual environments, caches, bytecode, generated RepoWiki data, worktree copies, binaries and vendored artifacts are outside ordinary source review.
- No tracked file was excluded. Secret configuration paths were not read; excluded categories and untracked paths are explicit in ledger metadata.
- Untracked local directories and user data are outside the audit boundary.

## Independent review coverage

| Batch | Scope | Result |
|---|---|---|
| I1 | 7 files | completed |
| I2 | 8 files | completed |
| I2 | 2 files | completed |
| I2 | 2 files | blocked |
| I2 durability follow-up | 8 files | multiple reviewer rounds completed; defects found and fixed |
| I2 quota capacity re-review | 1 file | narrow separate review completed after the final capacity fix |
| R1 | 15 files | completed |
| R10 | 123 files | Main-agent verification completed; candidates are recorded separately from findings. |
| R11 | 3 files | completed |
| R2 | 9 files | completed |
| R3 | 6 files | completed |
| R4 | 15 files | completed |
| R5 | 6 files | completed |
| R6 | 2 files | Main-agent source verification completed; rejected candidates are not findings. |
| R7 | 34 files | Main-agent source verification completed; rejected candidates are not findings. |
| R8 | 40 files | Main-agent source verification completed; rejected candidates are not findings. |
| R9 | 14 files | Main-agent source verification completed; rejected candidates are not findings. |
| RG | 8 files | Main-agent source verification completed for the exposed coverage gap. |
| I25 | 2 files | seven independent rounds; final `fixed_verified` |
| I26 | 5 files | four independent rounds; final `fixed_verified` |
| I27 | 4 files | independent `fixed_verified` |
| I28 | 8 files | independent `fixed_verified`; residual host access explicitly deferred |
| I29 | 4 files | independent `fixed_verified` |
| Final durability/transport follow-up | state/index and unknown-delivery consumers | independent `fixed_verified` after `needs_changes` cycles |
| Final review follow-up | TikTok/cache/docs regressions | independent `fixed_verified` |
| I30 documentation/source language | canonical docs and non-protected Python comments/docstrings | AST-equivalence; independent reviews with a documented correction cycle |
| Final whole-repository review | all current changes | initial `needs_changes`; six fixes independently re-reviewed `fixed_verified` |

R11 completed with repairs and a follow-up coverage-gap review. Static review does not verify live Telegram transport, deployed provider behavior, GitHub branch/secret policy, container memory limits, or restore behavior in production.

## Confirmed findings

Severity is assigned only to source-verified findings. Rejected candidates are recorded separately in the ledger and are not counted here.

| Severity | Confirmed-open findings |
|---|---:|
| P0 | 0 |
| P1 | 0 |
| P2 | 0 |
| P3 | 0 |

Total: 0 confirmed-open findings, 117 verified fixed, 20 deferred with recorded reason and 1 rejected candidate; exact ids in the ledger.

### R1

| ID | Priority | Status | Location | Finding |
|---|---|---|---|---|
| R1-01 | P1 | verified | `lumen_security.py:186,555,613` | Normalized leak detection and raw-text scrubbing disagree, so obfuscated identity or injected-payload echoes can pass through. |
| R1-02 | P1 | verified | `lumen_streaming.py:719,848` | Incremental streaming leak scan loses first-person context in long sentences. |
| R1-03 | P2 | verified | `lumen_admin.py:125,198` | Webhook deduplicates an update before final admission and can silently drop it on 503. Batch I19: the cap recheck now runs before dedup with no awaits in between; regression `test_webhook_503_does_not_burn_update_retry`. |
| R1-04 | P2 | verified | `lumen_streaming.py:1047` | Stream exception logging writes answer fragments into logs. |

### R2

| ID | Priority | Status | Location | Finding |
|---|---|---|---|---|
| R2-01 | P1 | verified | `lumen_chat_state.py:679,789` | Cancelled periodic flush can finish an older write after the final flush and overwrite newer state. |
| R2-02 | P1 | verified | `lumen_quotas.py:51,418` | Daily image Neuron budget is not reset at day rollover. |
| R2-03 | P1 | verified | `lumen_admin.py:359,377,406` | A partial non-empty export can replace the last good backup. |
| R2-04 | P1 | verified | `lumen_chat_state.py:789,943` | Chat and index writes are not atomic, so a new chat can become an orphan. |
| R2-05 | P1 | verified | `lumen_chat_state.py:753,813` | A failed chat deletion can still remove the ID from the index. |
| R2-06 | P1 | verified | `lumen_chat_state.py:1119,1172` | Pruning ignores queued chat-lock waiters and can delete context for an accepted message. |
| R2-07 | P2 | verified | `lumen_chat_state.py:274,984` | Malformed but valid JSON snapshots are not frozen and can be overwritten by empty state. |
| R2-08 | P2 | verified | `lumen_quotas.py:107,244,313` | Empty-string quota recovery doubles live in-memory counters. |
| R2-09 | P2 | verified | `lumen_message_core.py:1009,1457` | Ordinary text quota is checked and recorded non-atomically across chats. |
| R2-10 | P2 | verified | `lumen_commands.py:954,1398` | Draw-more callback can use a prompt cleared by /reset and can generate twice on rapid taps. |
| R2-11 | P2 | verified | `lumen_quotas.py:698,724` | A refund from the previous quota day can subtract a new day's reservation. |
| R2-12 | P2 | verified | `lumen_quotas.py:130,203` | Structurally malformed quota buckets clear the write guard and can lose bans. |
| R2-13 | P2 | verified | `lumen_chat_state.py:1078,1133` | get_state can prune a newly created chat and then raise KeyError. |
| R2-14 | P3 | verified | `lumen_quotas.py:48,309,746,782,827,944,1010` | MAX_USER_DAILY_KEYS was not a hard bound. |
| R2-15 | P2 | verified | `lumen_chat_state.py:1903,1915,1924` | A record-local legacy restore exception enables the global write freeze. |
| R2-16 | P2 | verified | `lumen_chat_state.py:1848,1864,1917,1924` | A skipped malformed legacy record is omitted from the new index and becomes unreachable after restart. |
| R2-17 | P1 | verified | `lumen_chat_state.py:1408,1444,1693` | A pruned chat can return before deletion succeeds; stale tombstones then delete its recreated history. |
| R2-18 | P2 | verified | `lumen_chat_state.py:1672,1685` | A stale save superseded by deletion is requeued forever even though the chat no longer exists. |
| I2-CAP-01 | P2 | verified | `lumen_quotas.py:34,35,50` | Reviewer-found: the shared 100k-node JSON guard rejected the supported 20,000-user quota bucket; the quota-specific limit is now 200k. |

### R3

| ID | Priority | Status | Location | Finding |
|---|---|---|---|---|
| R3-02 | P2 | verified | `lumen_commands.py:684,732` | Cancellation after a successful draw send refunds the general slot but skips state persistence. Batch I19: marks and `mark_state_dirty` precede the keyboard await, `sent_ok` stops the slot refund after a paid send; regression `test_draw_cancellation_after_send_keeps_marks_and_slot`. |
| R3-03 | P3 | verified | `lumen_commands.py:900` | Cancellation during draw revert left a visible restored photo with stale state. Fixed in batch I23: state commit moved to the synchronous window after the confirmed resend; the keyboard strip is now last and cosmetic. |
| R3-04 | P2 | verified | `lumen_commands.py:2140` | Pick token is consumed before chat-lock admission. Batch I19: token pop and keyboard removal moved inside the chat lock; the independent review caught a double-tap regression (get and pop split by awaits), fixed with a pop-guard and `test_pick_callback_double_tap_runs_core_once`. |
| R3-05 | P2 | deferred | `lumen_routes.py:329,381` | Non-stream answers enter history before confirmed delivery. |

### R4

| ID | Priority | Status | Location | Finding |
|---|---|---|---|---|
| R4-01 | P1 | verified | `lumen_message_core.py:997` | Passive group background can trigger paid memory summarization outside quotas. |
| R4-02 | P2 | verified | `lumen_search.py:1647,1676,1690` | Brave and Exa API keys can follow a cross-origin redirect. Fixed in batch I20: `allow_redirects=False` on all three keyed calls; `test_keyed_provider_requests_never_follow_redirects` red/green, independently reviewed. |
| R4-03 | P2 | verified | `lumen_transport_calls.py:212` | Transport collapses unknown delivery outcomes into None, causing duplicate media and incorrect refunds. Fixed in batch I21: `_tg_call` returns the `_TG_UNKNOWN` sentinel on unknown delivery; all consumer lifelines (media/dedup sender, refund gates, coverless re-send, streaming placeholder, `StatusMessage.start`/`start_action`, continuation sends) normalize or test it; three independent review rounds, full 97-site sweep clean. |
| R4-04 | P2 | verified | `lumen_commands.py:439,608`; `lumen_search.py:244-270` | Global image and keyed-search budgets are not atomic across awaits. Fixed in batch I22 with synchronous provisional pools. The final whole-repository review found a residual post-midnight refund defect; I28-bound follow-up now stores day and actor in the search-credit token, rejects old-day refunds before touching counters, and has red mutation evidence plus independent `fixed_verified`. |
| R4-05 | P2 | verified | `lumen_commands.py:857` | Paid draw prompt preparation runs before draw quota admission. Fixed in batch I20: `_draw_series_refusal_certain` consults the daily limits before the paid rewrite, and `inline_draw` still prepares the prompt itself if the peek misses; `test_variants_skip_paid_prepare_when_draw_limit_hit` red/green, independently reviewed. |
| R4-06 | P3 | verified | `lumen_tiktok_flow.py:426` | TikTok slides are sent as photos without image validation. |
| R4-07 | P3 | verified | `lumen_tiktok_flow.py:140` | Nested TikWM fields are used without type validation. |
| R4-08 | P3 | verified | `lumen_tiktok_flow.py:177` | TikTok logs contain user text and full URLs. |
| R4-09 | P3 | verified | `lumen_images.py:104,439` | Malformed Cloudflare moderation errors can bypass terminal moderation handling. |
| R4-10 | P3 | deferred | `lumen_search.py:1700` | Emergency HTTP paths weaken network protections. |

### R5

| ID | Priority | Status | Location | Finding |
|---|---|---|---|---|
| R5-01 | P2 | deferred | `lumen_formatting.py:639` | Nested emphasis produces crossed HTML tags. |
| R5-02 | P2 | deferred | `lumen_formatting.py:543` | Raw HTML inside fenced code is transformed before code extraction. |
| R5-03 | P2 | verified | `lumen_formatting.py:56` | Escaped table pipes split cells and lost data. Fixed with parity-aware splitting; I25 `fixed_verified`. |
| R5-04 | P2 | verified | `lumen_formatting.py:328` | Known LaTeX replacement corrupted unknown commands with the same prefix. Fixed with a command boundary; I25 `fixed_verified`. |
| R5-05 | P2 | deferred | `lumen_formatting.py:1002` | Long fenced code loses indentation at chunk boundaries. |
| R5-06 | P2 | verified | `lumen_status.py:259` | Activity prefix could overflow a valid answer. Fixed with UTF-16 composition checks and explicit table/overflow fallback reasons; I26 `fixed_verified`. |
| R5-07 | P2 | deferred | `lumen_formatting.py:979` | Unclosed fenced code is protected only in the first chunk. |
| R5-08 | P3 | verified | `lumen_formatting.py:581` | Common code-block info strings were not recognized. Fixed across HTML/Rich/plain/chunker; I25 `fixed_verified`. |
| R5-09 | P3 | verified | `lumen_formatting.py:44` | A one-hyphen table separator was not recognized. Fixed with width checks; I25 `fixed_verified`. |
| R5-10 | P3 | verified | `lumen_formatting.py:639` | Escaped Markdown emphasis was not respected. Fixed with parity-aware, collision-free single-character placeholders and link-label handling; I25 `fixed_verified` after seven rounds. |
| R5-11 | P3 | verified | `lumen_formatting.py:801` | Mixed math delimiters could leave a Rich placeholder. Fixed by extracting bracket math first; I25 `fixed_verified`. |
| R5-12 | P3 | verified | `lumen_rich.py:213` | A successful not-modified response was treated as failure and stripped formatting. Fixed by the local success adapter; I26 `fixed_verified`. |
| R5-13 | P3 | verified | `lumen_status.py:52` | Status and guest-answer sends did not disable link previews. Fixed and payload-tested; I26 `fixed_verified`. |

### R6

| ID | Priority | Status | Location | Finding |
|---|---|---|---|---|
| R6-01 | P2 | deferred | `proxy/proxy.ts:323,332,346` | The /read DNS check is not bound to the subsequent fetch, allowing DNS rebinding. Deferred in batch I18: neither Deno nor Workers fetch can pin the checked answer; window documented in code and ARCHITECTURE.md. |
| R6-02 | P2 | verified | `proxy/proxy.ts` warnOnTruncation | A close-truncated /read response used to drain upstream to EOF; batch I18 cancels the upstream at the cap in both relay copies. |
| R6-03 | P2 | verified | `proxy/proxy.ts` handleReadPage | Rejected /read responses now cancel the unread upstream body (415, non-200; the Workers copy also on 3xx), batch I18. |
| R6-04 | P2 | verified | `proxy/proxy.ts` handleReadPage | The /read timeout now starts with the request, is re-checked after DNS and races the resolution itself (review fix in batch I18), in both copies. |
| R6-05 | P3 | verified | `proxy/proxy.ts:266-302` | The IPv6 classifier now rejects the IANA special-purpose ranges inside 2000::/3 (documentation, benchmarking, ORCHIDv2, RTBH, reserved 3000::-3fff::, DCTP), mirrored in the dormant Workers port. |

### R7

| ID | Priority | Status | Location | Finding |
|---|---|---|---|---|
| R7-01 | P1 | verified | `tests/conftest.py:18,39,129` | Offline isolation is enforced for inherited configuration, DNS, synchronous connects and asyncio connects. |
| R7-02 | P2 | verified | `tests/bot_test_helpers.py:295-311` | The SSE fake now exposes one single-use body iterator. |
| R7-03 | P2 | verified | `tests/test_bot_agent_turn.py:322-359` | Search-failure coverage now asserts the production status object and exactly one closed fallback step. |
| R7-04 | P2 | verified | `tests/test_bot_state.py:1923-1971` | The logger regression now executes the real `__main__` entrypoint without starting the bot. |
| R7-05 | P2 | verified | `tests/test_bot_state.py:4500-4542` | The startup-alias regression now requires the exact top-level `_sys.modules.setdefault("bot", _sys.modules[__name__])`, its preceding import, no keywords, and no competing `sys`/`_sys` bot-module defaults. |
| R7-06 | P2 | verified | `tests/test_lumen_search.py:113-128` | The truncation regression asserts the cap, five complete packed rows, first-row content and exact retained/omitted URL pattern. |
| R7-07 | P2 | verified | `tests/test_lumen_agent.py:326-408` | The empty-search and normalized-query regressions prove the explicit tool marker, result state, assistant/tool pairing and successful normalized cache delivery. |
| R7-08 | P2 | verified | `tests/test_lumen_agent_gemini.py:14-373` | Request-contract fakes plus tool-history, empty/duplicate budget-filter, dedup, status/stop/deadline, builder-failure and step-cap regressions prove the loop wiring; thirteen production mutations rejected. |
| R7-09 | P2 | verified | `tests/test_bot_commands.py:4750-4791` | The ordered-kwargs fake plus delivery-result, byte, filename, chat_id, caption and markup assertions prove photo-first fallback; six production mutations rejected. |
| R7-10 | P2 | verified | `tests/test_lumen_router_config.py:248-346,1060-1075` | Build-route tests compare whole routes against pinned literal lists; three chain mutations caught, LINK copies pinned with a named source. |
| R7-11 | P3 | verified | `tests/bot_test_helpers.py:402-424`, `tests/test_bot_tiktok.py:699,773,849,911,954,1739` | The E2E TikWM fake now records requests and rejects repeats or foreign hosts; six E2E tests assert exactly one www-first request. Three mutations caught. |
| R7-12 | P3 | verified | `tests/conftest.py:56-165`, `tests/test_audit_fixes_17_11.py:1764-1800` | The autouse guard now snapshots and restores stream-stop flags, lock-waiter counters, chat locks, admin webhook dedup and `_http_session`; the leaked aiohttp 3.14 coroutine close is awaited in a fresh loop inside try/finally. Probe/checker pair plus three guard mutations. |
| R7-13 | P3 | rejected | `tests/test_bot_routes.py:1123,1151` | Measured: the fakes' sleeps are cancelled by the production timeout (tests cost 0.54s/0.61s, the 0.5s `_attempt_timeout` floor), behavioral asserts fail first on any timeout regression, and wall-clock margins are 5-10x. A fake clock would need a production time seam — not worth P3. |
| R7-14 | P3 | verified | `tests/test_lumen_images.py:198-217` | The seed test now patches the `secrets` seam with a scripted draw and asserts each call emits exactly its own `randbelow+1` in the URL — no dependence on two random values differing. Two mutations caught (constant seed; dropped `+1`); reviewer fixed_verified, one comment NIT applied. |
| R7-15 | P3 | verified | `tests/test_bot_commands.py:987-1035,1151-1201` | Both scheduler-order sleeps replaced with asyncio.Event start barriers (second site is `test_inline_draw_queue_wait_is_bounded` — reviewer MINOR, naming corrected); disabling the in-flight guard makes the test stop passing (caught), restore byte-identical -> 2 passed; reviewer fixed_verified. |
| R7-16 | P3 | verified | `tests/test_audit_fixes_17_11.py:570-602` | The index-recovery test now pins the whole retry contract: exactly one fetch with a fresh empty pending set, recovered ids applied to that same pending object, payload saved once under the current generation, JSON-parseable body. M1 (drop apply) and M2 (gate loses the load-failure term) both red; reviewer fixed_verified, MINOR flag-reset applied. |
| R7-17 | P3 | verified | `tests/test_bot_transport.py:348-381` | The transport test now pins the TCPConnector construction contract (family, limit, ttl_dns_cache, resolver type) via a kwargs spy instead of reading a private aiohttp field. |
| R7-18 | P2 | verified | `requirements-dev.txt:14,15` | The audit-added workflow test imports PyYAML without declaring the development dependency. |

### R8

| ID | Priority | Status | Location | Finding |
|---|---|---|---|---|
| R8-01 | P1 | deferred | `eval/harness_eval.py` `tests/test_eval_harness.py` `eval/README.md` | Environment-name scrubbing and shell denies reduce exposure but are not OS isolation: agent-controlled code can still read host files by absolute path. The residual risk is explicitly deferred for the local-only evaluation harness; I28 adds a fail-closed `HARNESS_UNSAFE_HOST_ACK=1` preflight and requires a disposable OS-isolated host without credentials for safe execution. Production bot runtime is not exposed to this harness. |
| R8-02 | P1 | verified | `.github/workflows/state-backup.yml:20,24,38,80` | Secret-bearing manual workflows can run from modified non-main refs without a ref guard. |
| R8-03 | P2 | deferred | `.github/workflows/persona-eval.yml:53,60,69,92` | The persona workflow uses the wrong SDK, omits Munito credentials, and can finish green without valid scores. |
| R8-04 | P2 | verified | `.dockerignore:35,42` | The Docker context does not exclude all private runtime traces and state. |
| R8-05 | P2 | verified | `.github/workflows/state-backup.yml:55,84,108` | The backup workflow accepts partial or structurally invalid snapshots. |
| R8-06 | P2 | verified | `eval/harness_eval.py:263,270,393,433,452,460,503,507` | Harness solved status ignores pyflakes, agent exit codes, and pytest process failure. |
| R8-07 | P2 | verified | `eval/harness_eval.py:218,244,541,562` | The shared harness cache has a temp-file race and incomplete cache key. |
| R8-08 | P2 | deferred | `.github/workflows/deploy-proxy.yml:87,108` | Proxy deployment is a green no-op unless DENO_PROXY_DEPLOY=1 is set. |
| R8-09 | P2 | deferred | `Dockerfile:6,13,24,26` | Docker builds are not reproducible. |
| R8-10 | P2 | deferred | `eval/persona_models.py:568,585,731,735` | Persona score parsing and report dataset selection can produce incorrect results. |
| R8-11 | P2 | deferred | `eval/live_smoke_probe.py:59,88,90,103,133,140` | The live-smoke probe measures unrelated Telegram traffic. |
| R8-12 | P3 | verified | `eval/persona_models.py:648,692` | Provider and target errors can still return exit code zero. |
| R8-13 | P3 | verified | `eval/persona_questions_casual.json:8,63` | The casual persona dataset omits language metadata and is always scored script-invalid. |
| R8-14 | P3 | deferred | `.github/workflows/persona-eval.yml:12,18,58,60,71,73` | The persona workflow contains obsolete provider keys and a nonexistent probe command. |
| R8-15 | P3 | deferred | `worker/wrangler.toml:6,7` | The Wrangler secret comment does not match any workflow. |
| R8-16 | P3 | verified | `eval/persona_models.py:159,167` | Evaluation code contains a hardcoded Windows path belonging to the owner. |
| R8-17 | P3 | deferred | `eval/README.md:54,55` | Evaluation documentation claims isolation that the harness does not implement. |

### R9

| ID | Priority | Status | Location | Finding |
|---|---|---|---|---|
| R9-01 | P1 | verified | `README.md:101,149` and the former Russian mirror, `docs/ARCHITECTURE.md:342,344,346` | Documentation promised automatic proxy deployment although deploy steps are disabled by default; the canonical English README/ARCHITECTURE now state the DENO_PROXY_DEPLOY gate and the manual ship. I30 removed the redundant Russian mirror. |
| R9-02 | P1 | verified | `README.md:149` → `.github/workflows/persona-eval.yml:58-61`, `docs/PROMPT_EVAL.md:43-47` | Documentation described a persona-eval workflow that could not run its default Munito targets; the step now passes MUNITO_API_KEY (repo secret must exist — owner check). |
| R9-03 | P2 | verified | `docs/ENVIRONMENT.md:70` | The Stop button is documented as always on; the retired environment switch cannot disable it. |
| R9-04 | P2 | verified | `docs/ARCHITECTURE.md:106,110` | `read_page` is documented with provenance, quotas, relay limits and fallback behavior. |
| R9-05 | P2 | verified | `docs/ARCHITECTURE.md:30` | The search chain now names only `gemini-2.5-flash-lite` as the current search-grounding head. |
| R9-06 | P2 | verified | `docs/ENVIRONMENT.md`, “Memory and photo context” | Photo-context values moved from hardcoded limits to the environment-variable reference. |
| R9-07 | P2 | verified | `README.md:68` | Deployment now uses the tested CI-to-Space path; direct Space pushes are an owner-only emergency exception rejected by the sync lease. |
| R9-08 | P2 | verified | `SECURITY.md:3-5` | Vulnerability reports now use private reporting and never direct exploit details to a public issue. |
| R9-09 | P2 | verified | `SECURITY.md:15-18` | A leaked relay secret is described as relay access, not automatic possession of the Telegram bot token. |
| R9-10 | P2 | verified | `docs/PROMPT_EVAL.md:28,152` | Provider-backed measurements are no longer described as offline runs. |
| R9-11 | P2 | verified | `eval/README.md:57-77` | The runbook states that environment scrubbing and command denies are not OS or network isolation. |
| R9-12 | P2 | verified | `docs/ENVIRONMENT.md:135,142` | Catch-up timing now includes RTT and integer floor division. |
| R9-13 | P3 | verified | `docs/ENVIRONMENT.md:27` | `TELEGRAM_API_SMALL_BODY_HOSTS` is documented in the proxy configuration table. |
| R9-14 | P3 | verified | `docs/ENVIRONMENT.md:42,179` | Duplicate `ADMIN_SECRET_SEED` and `MAX_TOTAL_MEMORY_MESSAGES` rows were removed. |
| R9-15 | P3 | verified | `docs/ENVIRONMENT.md:24,32` | Proxy-secret documentation now matches the accepted 32 printable non-space ASCII characters. |
| R9-16 | P3 | verified | `docs/CLAUDE_REFERENCE.md:191-192` | The inline Stop button is documented as implemented, not a future step. |
| R9-17 | P3 | verified | `eval/README.md:27` | The OpenCode runbook includes the required `--format json` flag. |
| R9-18 | P3 | verified | `README.md:146` | The brittle partial test list was replaced by a reference to the authoritative audit inventory. |
| R9-19 | P3 | verified | `README.md:60` | Minute-level 429 cooldown and daily quota exhaustion are documented as separate states. |

### RG

| ID | Priority | Status | Location | Finding |
|---|---|---|---|---|
| RG-01 | P2 | verified | `lumen_agent.py:430,436,507,537` | Stop during planning did not prevent subsequent reads. Fixed with post-planning and pre-execution checks plus partial-evidence preservation; I27 `fixed_verified`. |
| RG-02 | P2 | deferred | `lumen_read_page.py:337,395` | The personal read limit is checked and recorded non-atomically across chats. |
| RG-03 | P3 | verified | `lumen_agent.py:475,504,580` | Duplicate queries consumed budget before deduplication. Fixed with normalized deterministic deduplication and cache-aware charging; I27 `fixed_verified`. |
| RG-04 | P3 | verified | `lumen_agent.py:307,428,468` | READ_PAGE_MAX_PER_TURN=0 still advertised read_page. Fixed by conditional tool advertisement; I27 `fixed_verified`. |
| RG-05 | P3 | verified | `lumen_agent.py:461,474` | An unknown tool name executed as web_search. Fixed by explicit name validation; I27 `fixed_verified`. |
| RG-06 | P2 | verified | `worker/worker.ts` warnOnTruncation | Worker close truncation drained upstream after the cap; batch I18 rewrote it to the cancelling reader (mirror of the Deno fix), node test red-then-green. |
| RG-07 | P2 | verified | `worker/worker.ts` handleReadPage | Worker rejected responses now cancel unread upstream bodies (415, non-200, 3xx), batch I18. |
| RG-08 | P2 | verified | `worker/worker.ts` handleRequest | Worker /fetch cap realigned to the Deno error semantics in batch I18; close remains only for /read; test rewritten to expect the rejection. |
| RG-09 | P3 | deferred | `worker/worker.ts:245,255` | The Worker read timeout starts after DNS resolution. |
| RG-10 | P3 | deferred | `worker/worker.ts:158,185` | The Worker IPv6 classifier accepts non-public documentation ranges. |
| RG-11 | P3 | verified | `worker/worker.test.ts:48,58` | The wrong-secret test omits the secret instead of testing a wrong non-empty value. |

## I1 privacy remediation

I1 addresses R1-01, R1-02, R1-04 and R4-08.

- Identity and injected-payload detection now fails closed when normalized detection and raw-text scrubbing disagree.
- Streaming retains sentence and long-range context, withholds unsafe prefixes, defers punctuation boundaries, and revalidates pre-freeze polish output.
- Invisible/default-ignorable characters and compatibility-width obfuscation are normalized before leak decisions.
- `/stop` polishes before editing and recalculates the covered continuation offset after text-removing transforms.
- Streaming exceptions log exception type and safe status metadata, not answer fragments.
- TikTok logging removes credentials, paths, queries and fragments from URLs; untrusted identifiers and exception bodies are reduced to safe types or validated decimal values.

Changed files:

- `lumen_chat_state.py` — fixed_verified
- `lumen_commands.py` — fixed_verified
- `lumen_quotas.py` — fixed_verified
- `lumen_security.py` — fixed_verified
- `lumen_streaming.py` — fixed_verified
- `lumen_tiktok.py` — fixed_verified
- `lumen_tiktok_flow.py` — fixed_verified
- `tests/conftest.py` — fixed_verified
- `tests/test_audit_fixes_17_11.py` — fixed_verified
- `tests/test_bot_commands.py` — fixed_verified
- `tests/test_bot_state.py` — fixed_verified
- `tests/test_bot_streaming.py` — fixed_verified
- `tests/test_bot_tiktok.py` — fixed_verified
- `tests/test_bot_tts.py` — fixed_verified
- `tests/test_lumen_security.py` — fixed_verified

### I1 verification

| Check | Result |
|---|---|
| Targeted I1 tests | 368 passed in 18.76s |
| `pytest -q` | 2066 passed in 42.33s |
| `pyflakes bot.py lumen_*.py system_prompt.py tests/*.py` | passed |
| `deno test proxy/proxy_test.ts` | 45 passed |
| `deno check proxy/proxy.ts` | passed |
| `deno lint proxy/proxy.ts` | passed |
| `python -m pip check` | passed |
| `git diff --check` | passed |
| Independent final review | `fixed_verified` |

The known baseline-only aiohttp shutdown logging warning remains. No live bot, provider, production admin endpoint or deployment was exercised.

## I2 state-integrity remediation

Independently verified: R2-01 through R2-18, R8-05 and reviewer finding I2-CAP-01. I2 is closed.

### Durable state and deletion

- The replacement journal is durable schema 1. It still accepts legacy string entries as replacements and adds exact `{"kind":"delete"}` entries. IDs and generations are validated without coercion; malformed, mismatched or oversized data fails closed before indexed snapshots are trusted.
- A replacement generation is persisted before the snapshot and embedded in that snapshot. Publication keeps the ID out of the index until the replacement is durable; journal clearance follows index publication. A lost clear response requires matching index and snapshot confirmation.
- Deletion is two-phase: durable delete intent, snapshot deletion, then durable index exclusion without the ID, and only then journal clearance. Recreation cancels stale tombstones and reserves a newer generation; delayed deletes and superseded saves cannot remove or endlessly requeue the newer chat.
- Startup finalizes journaled interrupted deletions, including IDs already absent from the index. A missing snapshot or a stale pre-replacement snapshot is finalized as deletion; malformed replacement data is quarantined and its remote key is not overwritten.
- Journal I/O uses `_replacement_journal_lock`, separately from the short `_replacement_state_lock` used on the event loop. The unresolved-ID union is capped by `_MAX_UNRESOLVED_CHAT_IDS = MAX_CHAT_LIMIT = 5000`; retries remain allowed, while new unresolved work fails closed at capacity.
- Valid-ID malformed legacy records stay discoverable and quarantined through the durable index and `legacy_pending` boundary. Unidentifiable IDs fail closed and preserve the legacy blob. This verifies R2-15 and R2-16 without freezing valid siblings.

### Quota and JSON validation

- Quota writes share `_quota_write_lock` and a mutation generation. A queued or cancelled older payload cannot overwrite the shutdown/newer snapshot, and dirty state is cleared only if its generation is still current.
- Async quota recovery offloads only I/O and parsing. Validated snapshot application and the live-outage merge run on the event loop, preserving mutations made while the worker read was pending; empty or malformed reads cannot self-merge or clear the write guard.
- Recursive finite-number checks were replaced by an iterative traversal with depth 256 and node limits. The shared default is 100,000 nodes; quota validation uses 200,000 because 20,000 daily user entries need about 120,000 nodes. The final capacity regression covers the full `MAX_USER_DAILY_KEYS` bucket.

### Export and backup

- `/export_state` refuses loading, failed state/quota loads, quarantined or unresolved chats, in-flight deletions and structurally invalid or non-finite snapshots with a retryable 503. It validates a detached, complete schema-1 snapshot before returning it.
- `state-backup.yml` independently validates the same export contract before replacing the last good backup. Executable jq parity tests cover valid snapshots, replacement generations, integral JSON numbers, malformed nested quota data, non-finite/overflow values, multiple documents and partial snapshots.

### Independent review history

The earlier blocked deletion-race re-review remains recorded. Subsequent independent reviewer rounds found multiple defects in journal ordering, restart finalization, stale replacement handling, lock separation, quota recovery and validation; these were repaired and re-reviewed. The last broad reviewer found the 20,000-user quota capacity mismatch. The one-line quota-specific node-limit fix and its new capacity test were then reviewed separately and returned `fixed_verified` for that narrow scope. This is not a whole-batch or whole-repository closure verdict.

### Regression and verification evidence

Retained regressions exercise journal write/clear loss, crash windows, periodic and shutdown restart finalization, stale pre-replacement snapshots, malformed/mismatched quarantine, event-loop lock separation, unresolved capacity, quota write generations, recovery merges during a pending read, iterative depth/node bounds, export refusal and jq parity. Red/green evidence is recorded per finding: deletion/recreation, stale-save retry, legacy quarantine and recovery defects failed or were reproduced before correction. The final quota-capacity test passed after the one-line fix.

| Check | Result |
|---|---|
| Full pytest before final quota-capacity change | 2216 passed in 45.67s |
| New full-capacity quota test | 1 passed |
| Subsequent fresh full pytest | 2217 passed in 43.56s |
| `pyflakes bot.py lumen_*.py system_prompt.py tests/*.py` after final change | passed |
| `git diff --check` after final change | passed |
| Workflow YAML parse | passed |
| `python -m pip check` | passed |
| Inventory validator | passed after ledger/report hash and status updates |

The fresh full suite retained the known aiohttp shutdown logging noise; it exited successfully. No network, live bot, provider, admin endpoint, deployment, push or merge was used.

Changed I2 files:

- `lumen_chat_state.py` — durable journal, deletion/replacement recovery, quarantine and unresolved cap.
- `lumen_state_storage.py` — schema-1 journal parsing, generation snapshots and iterative JSON validation.
- `lumen_quotas.py` — write lock/generation, event-loop recovery merge and quota-specific node capacity.
- `lumen_admin.py` — fail-closed complete export validation.
- `.github/workflows/state-backup.yml` — fail-closed jq contract before backup replacement.
- `tests/test_bot_state.py` — state, deletion, quota and capacity regressions.
- `tests/test_bot_admin.py` — incomplete/invalid export regressions.
- `tests/test_state_backup_workflow.py` — executable jq parity contract.
- `tests/conftest.py`, `tests/test_audit_fixes_17_11.py`, `tests/test_bot_commands.py`, `tests/test_bot_tts.py` — retained fixture and regression compatibility.

## Latest independently verified remediation batch

Verified: R4-01 through R4-09, R6-02 through R6-05, R8-02, R8-04, R8-06, R8-07, R8-12, R8-13, R8-16, R9-01, R9-02, RG-01, RG-03 through RG-08, RG-11, RG-12, R2-06, R2-09, R2-10, R2-14, R1-03, R3-02, R3-04, R3-07 through R3-13, R7-01 through R7-12, R7-14 through R7-18 (R7-13 rejected), all six I25 findings and all three I26 findings. R6-01 and the residual R8-01 host-access limitation are deferred with explicit reasons. Batches I1-I30 are closed.

- R4-01 (P1): passive group recording no longer calls paid summarization. Only overflow above `MAX_TOTAL_MEMORY_MESSAGES=200` enters `memory_pending`; 150 recent messages remain in history, and the next active trim summarizes. The reviewer found an initial 150-threshold regression; it was corrected and re-reviewed clean.
- R8-02 (P1): secret-bearing `state-backup` and `relay-deno` jobs require `github.ref == 'refs/heads/main'`; scheduled backup runs remain allowed. Executable YAML tests cover main/non-main dispatch and the schedule.
- R2-06 (P1): pruning treats normal and background chat-lock waiter counters as busy through the unlocked handoff. Independent probes confirmed both paths.
- R2-09 (P2): ordinary text reserves one slot before paid search/model work. A ContextVar and task done callback refund failure, cancellation and early return; success consumes once, including Gemini. Direct sequential/nested core calls reset `_ANSWER_COUNTED` and restore its token. Atomicity is single-process only. The reviewer found and corrected direct-call undercount; final narrow re-review was clean.
- R2-10 (P2): draw-more validation, token, prompt capture, admission and inflight state run under the chat lock and are rechecked after waiting. `/reset` and state replacement invalidate stale callbacks; rapid taps generate once. Independent review was clean.
- R1-03/R3-02/R3-04 (P2, batch I19): webhook admission order, draw cancellation accounting and pick token consumption. `webhook_handler` now rechecks the inflight cap before marking the update as a duplicate, with no awaits between the recheck, dedup and `create_task`, so a 503 leaves the update retryable instead of burning it (`test_webhook_503_does_not_burn_update_retry`, red on HEAD). `inline_draw` writes the version marks and `mark_state_dirty` before the keyboard-removal await and adds a `sent_ok` flag so a cancellation after the photo was accepted no longer refunds the already-paid daily slot (`test_draw_cancellation_after_send_keeps_marks_and_slot`, red on HEAD). `handle_pick_callback` moved the token pop and keyboard removal inside the chat lock so a busy chat or cancel no longer leaves a dead button (`test_pick_callback_busy_chat_does_not_run_core` extended, red on HEAD). The independent review caught a regression in that last fix — `get` and `pop` were split by awaits, so a double tap could run the core twice — and the implementer closed it with a pop-guard inside `_pick_admitted` plus `test_pick_callback_double_tap_runs_core_once` (red, await_count==2, on a guard-less copy); the re-review returned fixed_verified.
- R4-02/R4-05 (P2, batch I20): keyed-search redirect leak and paid prepare before quota admission. `_api_provider_snippets` now sets `allow_redirects=False` on the Tavily, Brave and Exa calls, so a keyed API key never follows a cross-origin 302 (`test_keyed_provider_requests_never_follow_redirects`, all three params red on the pre-fix copy). `inline_draw_variants` skips the paid Munito prompt rewrite when a consultative daily-limit peek (`_draw_series_refusal_certain`) proves the series will be refused anyway; the peek is advisory only — `inline_draw` keeps the atomic reserve as the actual verdict and prepares the prompt itself if the peek misses (`test_variants_skip_paid_prepare_when_draw_limit_hit`, red on a copy with the peek reverted). The independent reviewer confirmed both fixes, re-derived the second red case, and verified the peek/reserve arithmetic (`used >= limit` ⇔ `used+1 > limit` for a delta of one); the only note was a narrow day-rollover race where one variant's prep is paid before the refusal — self-correcting, accepted.
- R7-18 (P2): the audit-added workflow test imported undeclared PyYAML. This was confirmed; the owner explicitly approved `PyYAML==6.0.3`, which is now declared in `requirements-dev.txt`. CI dependency closure was verified.
- R7-14 (P3, batch I11): `test_pollinations_url_carries_seed_safe_and_nologo` assumed two random seeds must differ. The test now patches the `secrets` seam with a scripted draw (`randbelow` returning 11, then 22) and asserts each call emits exactly its own `+1` in the URL (`seeds == [12, 23]`) while keeping the real first-call checks for safe/nologo/enhance. Mutations: constant seed 42 -> 1 failed; dropped `+1` -> 1 failed; file 68 passed, 5 repeat runs stable. Production `lumen_images.py` untouched. Reviewer fixed_verified; comment NIT («collиднуться» -> «столкнуться») applied, the fake-ignores-`n` NIT rejected as optional for a P3 determinism fix.
- R6-05 (P3, batch I15): `isPrivateAddress` in `proxy/proxy.ts` enumerated most IPv6 special-purpose ranges but accepted the IANA-registry blocks inside 2000::/3 as public — 2001:db8::/32, 2001:2::/48, 2001:10::/28, 2001:1::1/128 and the reserved 3000::-3fff::. Six numeric bigint checks were added after the existing discard check, deliberately read as top-4-bits (3000::-3fff::) because the registry's "/3" spelling would swallow all of 2000::/3 including real public space. The reviewer's MINOR (2620:4f80::/32 DCTP) was applied the same batch after a fresh red probe; the NIT (unparseable address -> public) is rejected by design: the predicate also sees DNS hostnames, fail-closed would refuse every named host. The red regression extended the reserved-ranges deno test first (1 failed), then green; mutations M1-M5 (dropping each original added line) all turned the test red, files restored byte-identical. The same function is a copy (not an alias) in `worker/worker.ts`, updated identically (hunk-diff verified), with the mirror probes in `worker/worker.test.ts` including a public negative. Gates: deno test 45 + 16 passed, deno check both clean, deno lint proxy clean (worker has one pre-existing require-await at worker.ts:348, reproduced on HEAD — out of this finding's scope).
- R6-02/R6-03/R6-04 (P2, batch I18): the /read lifecycle in `proxy/proxy.ts` and its Workers mirror. `warnOnTruncation` was rewritten from a `pipeThrough` drain to an explicit reader: at the byte cap the upstream is cancelled instead of being read to EOF, mode "close" ends the /read stream cleanly and mode "error" still aborts /fetch. Every rejection in `handleReadPage` (415, non-200; plus 3xx in the Workers copy, which fetches with `redirect: "manual"`) cancels the unread upstream body. The 15 s deadline is created before the DNS step, re-checked after it and passed to fetch as the same signal; the batch review additionally raced the resolution against the deadline, so a hung `Deno.resolveDns` (bounded 3 s DoH on Workers) is cut off at `readTimeoutMs` — proven red on the pre-race copy. R6-05 leftovers found by the same review were fixed in place: the 2001:10::/28 comment was mislabeled ORCHIDv2 (it is ORCHID, RFC 7285) and the real ORCHIDv2 2001:20::/28 (RFC 9375) was not blocked; the Workers /fetch response cap silently ran in "close" mode while the Deno relay errors — the dormant copy was realigned to "error" and its test rewritten. R6-01 (DNS answer not pinned to the following fetch) is deferred with reason: Deno fetch and Workers offer no DNS pin, a manual connectTls HTTP client is disproportionate for the relay, the rebinding window stays narrow and is now documented at the call site and above. Four new Deno tests and four mirrored node tests, all red against the pre-batch HEAD first (3 of them failed there), green after; gates: deno test 49/49, node 20/20, deno check proxy+worker clean, deno lint proxy clean, full Python suite re-run green.
- R8-01 (P1, batch I16): `eval/harness_eval.py` handed the evaluated agent a full `os.environ` copy, and the judge (`pytest_state`/`pyflakes_state`) plus the post-agent `git status`/`git diff` in `keep_sandbox` silently inherited the same host environment — the owner's provider keys and tokens were reachable from agent-written code (a modified conftest or a `.git/config` filter driver). `agent_env` now filters variables whose names match `SECRET_ENV_RE` (API_KEY/KEY/TOKEN/SECRET/PASSWORD/PASSWD/CREDENTIAL/AUTH/AUTHORIZATION/WEBHOOK/DSN with separator boundaries), judge and sandbox-git calls pass the scrubbed env, and `bash` permission gained `cat`/`type`/`Get-Content *.env*` denies. First tests for the harness: `tests/test_eval_harness.py` (4 tests, red on the old code; mutations M1 full-copy, M2 no denies, M3 judge env dropped, M4 sandbox-git env dropped all turned them red, files restored byte-identical). Reviewer passes: first needs_changes (MAJOR post-agent `git()` full env; MINOR SENTRY_DSN/AUTH/TOKEN2 survived), both fixed in the batch; second pass fixed_verified, probes found no false positives on toolchain variables (PATH/HOME/SYSTEMROOT/TEMP/PYTHONPATH/NO_PROXY stay). Accepted as designed: URL variables without a secret-bearing name (UPSTASH_REDIS_REST_URL) survive per the repo policy that URLs are configuration; glued names without a separator (MYCREDENTIALS) survive; the sandbox is still not an OS jail — the agent can read the host `.env` by absolute path through `python -c` (recorded in eval/README.md as a limitation). Owner check after merge: one smoke `python eval/harness_eval.py run` to confirm the OpenCode CLI authenticates from its auth.json under the scrubbed env.
- R9-01 (P1, batch I17): The then-existing English README (lines 101 and 149) and Russian mirror (82 and 130) told readers a green CI run automatically ships the Deno relay and to "confirm both deploy steps finished as success", while `deploy-proxy.yml` gates both deploy steps behind `vars.DENO_PROXY_DEPLOY == '1'` — unset since the 16.11.2026 audit because new console tokens fail the old deployctl. The canonical English README and the stale Deploy-paths sentences in docs/ARCHITECTURE.md (lines 342/344/346, including "no token in the workflow") now state the real behavior: the job always runs, the steps stay disabled until relay migration, a skipped step never means a shipped proxy, and the ship is the owner's manual step. I30 removed the redundant Russian mirror. A new executable contract, `test_deploy_proxy_steps_are_gated_off_by_default` in `tests/test_ci_provider_contracts.py`, pins the gate so the docs and the workflow YAML cannot drift apart silently (repo precedent: tests/test_state_backup_workflow.py parses workflow YAML).
- R9-02 (P1, batch I17): `.github/workflows/persona-eval.yml` selected munito heads in its default `models` input but the "Persona eval" step passed only GEMINI/GROQ/OPENROUTER keys, so `eval/persona_models.py:320-322` aborted with "нет MUNITO_API_KEY" on the first target — the workflow could never run its own defaults. The step now maps `MUNITO_API_KEY: ${{ secrets.MUNITO_API_KEY }}` (protected file; the one-line plan is recorded in the change record), guarded by `test_persona_eval_passes_keys_for_its_default_providers`, which was red before the fix and is green after; MUNITO_BASE_URL needs no mapping (module default in persona_models.py:46-47). Reviewer MINOR-2 fix: docs/PROMPT_EVAL.md no longer claims the defaults "call retired targets and fail at parsing" — the real defect was the missing key. Not verified from this machine: whether the repository secret MUNITO_API_KEY exists (`gh secret list` returned 403); the owner must create it before the first workflow run, and a single manual persona-eval dispatch after merge is the remaining smoke check.
- R7-17 (P3, batch I14): `test_get_http_session_connector_ipv4_limit_and_public_resolver` reached into the private `connector._resolver` field, coupling it to aiohttp internals and leaving `limit`/`ttl_dns_cache` unpinned. It now wraps the real `aiohttp.TCPConnector` in a kwargs spy, builds the session through `lumen_transport_calls._get_http_session` (with `bot._http_session` nulled, session closed in `finally`) and asserts family=AF_INET, limit=40, ttl_dns_cache=300, resolver instanceof `_PublicOnlyResolver`. Mutations in `lumen_transport_calls.py` — dropping the resolver kwarg, limit 40->16 — each turn it red; the production file was restored byte-identical. Reviewer fixed_verified (focused 1 passed, whole file 36 passed); two NITs kept: the trivial `assert s is not None` still guards construction, and the kwargs-only spy safe-fails if production ever switches to positional args.
- R7-16 (P3, batch I13): `test_flush_retries_index_after_load_failure_without_dirty_flag` asserted only that `_fetch_index_ids` was called. It now drives three recording fakes (fetch returns {4242}, apply and save record arguments) and pins: one fetch with a fresh empty `migration_pending`, the same pending object reaching `_apply_recovered_ids` with the recovered ids, exactly one `bot._save_chat_index_payload` under the current `_index_generation`, and a JSON-parseable payload. Mutations in `lumen_chat_state.py` — removing the apply call, and dropping the `_state_load_failed` term from the flush gate — each turn it red; the production file was restored byte-identical (hash `cea05f58…` unchanged). Reviewer fixed_verified; MINOR (flag left set) applied as an explicit reset at test end; NIT (bare `json.loads`) kept by design. Reviewer-logged residual gap, out of this finding's scope: the fetch-returns-None skip-save branch is not pinned here.
- R7-15 (P3, batch I12): the two inline-draw concurrency tests relied on `await asyncio.sleep(0)` to let the first task enter its slot — a scheduler-order assumption. Each fake generator now sets an `asyncio.Event` after the production guard/semaphore has really taken the slot, and the scenario awaits `asyncio.wait_for(..., 5.0)` before issuing the second draw; a stuck barrier fails in 5 s instead of hanging. Mutation: disabling the per-user in-flight guard in `lumen_commands.py` (line 475 -> `if False:`) makes the test block forever (regression caught, timeout-guarded run), restore byte-identical -> 2 passed; assertions untouched (refusal+refund, bounded queue+slot release). Reviewer fixed_verified; MINOR corrected the second test's name in evidence (queue-wait, not send-failure); NIT about list+event duplication kept as-is (both are used).
- R7-12 (P3): `_bot_global_state_guard` in `tests/conftest.py` now snapshots and restores the six confirmed leak points — `_STREAM_STOPS`, the two lock-waiter counters, `_chat_locks` (test-loop locks cannot outlive their loop, so foreign entries are dropped), `lumen_admin` webhook dedup and `bot._http_session`. The first fix attempt called the session close synchronously and the regression pair failed: aiohttp 3.14.5 `ClientSession.close()` is a coroutine and a bare call is a silent no-op, so the guard now awaits it via `asyncio.run` inside try/finally (review MINOR: a hostile close must not poison later snapshots). The probe/checker pair in `tests/test_audit_fixes_17_11.py` leaks all six shapes deliberately and asserts the restore; removing any restore block turns the checker red (M1/M2/M3). After the fix the focused run shows no "Unclosed client session" noise.
- R7-11 (P3): `_FakeTikTokSession` now records every requested URL and rejects a repeated TikWM call or a foreign host; the six E2E TikWM tests hold one persistent session and assert exactly one www-first request. The class docstring states that production swallows the raise, so asserting `session.requests` is mandatory. Mirror-swap, swallowed-success and no-accounting mutations rejected (7/13/6 failures); a post-restore red run was proven to be a stale equal-length `__pycache__` artifact, not a code regression.
- R7-10 (P2): the router tests now pin expected munito/gemini model lists as literals instead of importing the constants under test; the pdf/sticker leftover was pinned too, and a comment records that LINK chains are copies of heavy/search. Three chain mutations rejected.
- R7-09 (P2): the draw document-fallback regression now records ordered send calls and asserts the delivered return value, identical BufferedInputFile bytes/filename, chat_id, caption and reply markup parity; a document-first rewrite is caught. Six mutations rejected.
- R7-08 (P2): the Gemini fakes now validate the full request contract (model, seeded contents identity, exact web_search declaration with name/description/schema, automatic function calling disabled) and record every call; six added regressions cover the tool history, empty/duplicate query cleaning, status preamble and user stop, expired deadline, builder failure and the step cap; a brittle config-identity assert was dropped. Each point is proven by a rejected mutation.
- R7-07 (P2): empty web-search results now carry an explicit no-results marker and full result-state assertions. A companion mixed-spacing case proves the normalized cache result reaches the assistant/tool transcript; removing normalization falsely reports success as no results.
- R7-04 (P2): the logger regression now loads `bot.py` as `__main__` in an isolated subprocess, proves the entrypoint guard is called once and leaves Bot/client uninitialized. The `__name__` mutation is rejected.
- R7-03 (P2): the search-failure test now verifies the production status object, a live query phase and exactly one completed fallback step. Independent review closed the indirect-fake weakness.
- R7-02 (P2): `_FakeSSEResponse` stores one body iterator, so a second traversal is empty like a consumed real response. The replay regression failed before the fix; independent review found no remaining defects.
- R7-01 (P1): test configuration is authoritative and disables inherited Upstash/provider values. An autouse guard blocks external DNS, synchronous connects and both selector/Windows Proactor asyncio connects; loopback and AF_UNIX remain available. Three independent review rounds closed DNS omissions, weak negative tests and the Proactor bypass.
- R2-14 (P3): `MAX_USER_DAILY_KEYS=20000` is now a hard bound. New accounts are refused when all slots hold usage or bonus; only idle entries are reclaimed. Persisted oversized buckets fail validation, unsafe recovery restores local quota while preserving remote bans, and recovery prepares all idle victims in one linear pass. Text, TTS and draw distinguish capacity/integrity refusal from personal limits; reservations check only the requested quota category. The final independent review found no remaining defects after 10,000 random valid merges and a 20,000-entry merge completing in 0.025 seconds.

### I25-I27 final remediation

- I25 (R5-03, R5-04, R5-08, R5-09, R5-10, R5-11; `fixed_verified`) repaired escaped table pipes, LaTeX command boundaries, common fence info strings, one-hyphen table separators, escaped emphasis and mixed Rich math delimiters. Seven review rounds found and fixed restoration-order, adjacent-placeholder, key-expansion, link-label and post-LaTeX collision defects. The final marker is one absent private-use or unassigned character, so deletion/concatenation cannot create it; exhaustion fails explicitly. Focused formatting suite: 175 passed. Reviewer measured 4,096 characters at 0.0003 s and 8,000 escapes at 0.0021 s.
- I26 (R5-06, R5-12, R5-13; `fixed_verified`) made activity composition UTF-16-safe, distinguished table/no-prefix/overflow fallbacks, guarded Rich-only table overflow, treated Telegram not-modified as local success and disabled link previews for status/guest sends. Focused status/streaming suite: 114 passed. The reviewer reproduced the 4,124-unit Rich overflow and verified the 4,084-unit no-prefix fallback through a mounted aiogram `Message`.
- I27 (RG-01, RG-03, RG-04, RG-05; `fixed_verified`) added post-planning/pre-execution stop checks, preserved partial evidence, deduplicated normalized queries before budget slicing, avoided charging cached repeats, rejected unknown tool names and hid `read_page` at zero budget. Focused agent suites: 44 passed plus six independent probes.
- I28 (R8-04, R8-06, R8-07, R8-12, R8-13, R8-16, RG-11; `fixed_verified`) added the fail-closed unsafe-host acknowledgement, complete solved-status gate, complete cache key with unique atomic temp writes, nonzero persona error exits, Russian dataset metadata, portable env-path derivation, Docker privacy exclusions and a real wrong-secret test. R8-01 is honestly deferred: environment scrubbing is not OS isolation.
- I29 (R4-06, R4-07, R4-09; `fixed_verified`) validates static slide signatures and filenames, normalizes malformed TikWM metadata and makes Cloudflare error extraction type-safe while preserving terminal moderation classification. Focused suites: 260 passed; the pre-fix regression selection was 15 failed / 7 passed.
- The R4-04 follow-up binds search refunds to the original day and actor; an in-memory mutation of the old refund reproduced the new-day counter corruption. RG-12 enforces the 20,000-entry backup quota bound with a red/green executable workflow test.
- The final whole-repository review returned `needs_changes` for malformed TikWM containers and a parallel targets-cache lost-update race. Both were fixed with red/green regressions and independently re-reviewed as `fixed_verified`; the cache now serializes read-merge-write as well as atomic replacement.
- I30 translates canonical technical documentation to US English, removes the redundant Russian README mirror, and normalizes non-protected Python comments/docstrings. The application verifies that executable ASTs are unchanged. Independent review found four semantic/formatting translation defects; these were corrected against the original wording, and token-based passes normalized 179 comment continuations in total with unchanged ASTs and non-comment code tokens. Protected files retain their existing language because repository policy requires a separately approved plan.

The retained regressions were red on the pre-fix behavior and green after correction. Review-found defects remain in the ledger/report history rather than being treated as clean first passes.

| Check | Result |
|---|---|
| `pytest -q` | 2511 passed, 1 skipped |
| `pyflakes bot.py lumen_*.py system_prompt.py tests/*.py` | clean |
| `git diff --check` | clean |
| Workflow YAML parse | all workflow YAML parsed |
| `python -m pip check` | clean |
| Inventory validator | passed after ledger/report hash and status updates |

Known non-failing aiohttp/logging shutdown noise remains. No live service, deployment, commit or push was used.

Changed batch files:

- `.github/workflows/state-backup.yml`, `.github/workflows/relay-deno.yml` — trusted-ref job guards.
- `lumen_chat_state.py`, `lumen_message_core.py` — waiter-aware pruning, passive memory queue and text reservation lifecycle.
- `lumen_quotas.py` — atomic reservation/refund support and task binding.
- `lumen_commands.py` — locked draw-more admission and stale-callback invalidation.
- `requirements-dev.txt` — approved PyYAML development dependency.
- `tests/bot_test_helpers.py`, `tests/test_bot_streaming.py` — single-use SSE response body and replay regression.
- `tests/conftest.py`, `tests/test_offline_isolation.py` — authoritative offline configuration and sync/async network guards.
- `tests/test_bot_state.py`, `tests/test_bot_routes.py`, `tests/test_bot_commands.py`, `tests/test_lumen_agent.py`, `tests/test_state_backup_workflow.py` — executable regressions.
- `.dockerignore`, `eval/harness_eval.py`, `eval/persona_models.py`, `eval/persona_questions_casual.json`, `eval/README.md`, `worker/worker.test.ts`, `tests/test_eval_harness.py`, `tests/test_ci_provider_contracts.py` — I28 privacy and eval correctness.
- `lumen_tiktok_flow.py`, `lumen_images.py`, `tests/test_bot_tiktok.py`, `tests/test_lumen_images.py` — I29 media/moderation validation.
- `lumen_search.py`, `tests/test_lumen_search.py`, `.github/workflows/state-backup.yml`, `tests/test_state_backup_workflow.py` — final rollover and backup-capacity corrections.

## Subtractive and architecture candidates

These are verified candidates, not completed removals. Each requires the listed migration, policy decision or targeted test before execution.

| ID | Decision | Location | Candidate | Trigger |
|---|---|---|---|---|
| R10-01 | REMOVE | `bot.py` | _DOTS_FRAMES | safe_after_targeted_test |
| R10-02 | REMOVE | `lumen_search.py` | _QUESTION_WORDS_RE;_PRICE_RE | safe_after_targeted_test |
| R10-03 | REMOVE | `eval/persona_models.py` | JUDGE_MAX_TOKENS;_SEARCH_MARKER | safe_after_targeted_test |
| R10-04 | REMOVE | `bot.py` | compatibility_reexports | protected_file_plan_and_test_migration |
| R10-05 | REMOVE | `lumen_chat_state.py` | _upstash_delete | safe_after_targeted_test |
| R10-06 | REMOVE | `tests/conftest.py` | obsolete_environment_stubs | I7 |
| R10-07 | REPLACE | `lumen_media_flow.py` | _resolve_edit_reference | test_migration_required |
| R10-08 | REPLACE | `lumen_commands.py` | inline_draw_tuple_compatibility | test_migration_required |
| R10-09 | IMPROVE | `lumen_typing_pace.py` | clear_edit_gate | I3_or_I4_lifecycle_batch |
| R10-10 | IMPROVE | `lumen_agent.py;lumen_agent_gemini.py` | duplicated_agent_limits | I10_after_behavior_batches |
| R10-11 | IMPROVE | `lumen_state_storage.py` | storage_docstrings | I9 |
| R10-12 | DEFER | `lumen_router_config.py` | TEXT_MODEL_ORDER | owner_approved_protected_change |
| R10-13 | DEFER | `lumen_chat_state.py` | direct_storage_calls | I2_test_migration |
| R10-14 | DEFER | `lumen_commands.py;lumen_tts.py;bot.py` | tts_reexport_roundtrip | I10_test_migration |
| R10-15 | DEFER | `lumen_router_config.py` | copied_gemini_chains | accepted_risk |
| R10-16 | DEFER | `lumen_tts.py` | legacy_generate_content_fallback | needs_sdk_contract_validation |
| R10-17 | DEFER | `.github/workflows/deploy-proxy.yml;proxy/proxy.ts` | relay_duplication | needs_owner_decision |
| R10-18 | DEFER | `bot.py` | bot_facing_test_seams | I10_test_migration |

## Additional findings

| ID | Priority | Status | Location | Finding |
|---|---|---|---|---|
| I2-CAP-01 | P2 | verified | `lumen_quotas.py:34,35,50` | The shared 100k-node JSON guard rejected the supported 20,000-user daily quota capacity. A quota-specific 200k node budget preserves the shared default and depth limit. |
| R3-06 | P2 | verified | `lumen_commands.py` (`revert_draw_version`) | A falsy resend result, including `_TG_UNKNOWN`, could commit an anchor for a message that may not exist. I24 aborts the commit and has independent red/green evidence. |
| RG-12 | P3 | verified | `.github/workflows/state-backup.yml:99,100` | Backup validation accepted more `user_daily` entries than the runtime hard cap. The jq contract now enforces 20,000; a 20,001-entry regression was red before the fix and green after. |
| DOC-01 | P3 | deferred | `system_prompt.py`; `lumen_security.py`; `lumen_router_config.py` | Protected files retain 52 Russian docstrings and 428 Russian comments. They were not edited because repository policy requires a separately approved plan; all non-protected Python comments/docstrings are normalized. |
| R3-07 | P1 | verified | `lumen_chat_state.py` | New-chat index publication could outrun the durable snapshot. The replacement journal now precedes the snapshot; outage recovery preserves existing remote snapshots. Final independent review: `fixed_verified`. |
| R3-08 | P1 | verified | `lumen_transport_calls.py`; `lumen_streaming.py` | Post-response decode failures could trigger duplicate sends, retries or refunds. Unknown delivery blocks Rich/plain/media fallback, duplicate streaming sends and final-tail overwrite; final independent re-review is recorded in the ledger. |
| R3-09 | P2 | verified | `lumen_read_page.py` | Parallel chats could exceed the personal read cap, and empty pages consumed it. Reservations are atomic; failure, cancellation and empty text refund the personal share. |
| R3-10 | P2 | verified | `eval/persona_models.py`; `eval/harness_eval.py` | Malformed judge output fabricated scores and batch/check hid failures. Parsing is fail-closed and aggregate failures return nonzero. |
| R3-11 | P2 | verified | `.gitignore` | Private runtime histories, state, backups and devtrace variants are now covered by executable ignore contracts. |
| R3-12 | P2 | verified | `.dockerignore` | Audit artifacts and private runtime state are excluded from the Docker context while source remains included. |
| R3-13 | P3 | verified | `tests/test_eval_harness.py` | The atomic-write test now serializes only the final Windows rename; 20 repeated runs passed. |
| DOC-02 | P3 | verified | `docs/ENVIRONMENT.md` | Proxy deployment gating and the always-on Stop control now match workflow and code. |
| DOC-03 | P3 | verified | `docs/ARCHITECTURE.md` | The stale statement that `read_page` was absent no longer contradicts its implemented route. |

## Owner policy decisions

The following policy decisions are recorded for traceability; resolved items remain listed, while unresolved items continue to affect I2-I10 design:

- Fail-closed readiness for unreadable state and quota records.
- New-chat behavior during a storage outage.
- Backup completeness, retention, RPO and RTO.
- Orphan reconciliation strategy and local-file backend support.
- Hard quota-key bound and waiter-aware pruning.
- Day/generation tokens for refunds.
- Legacy compatibility retention.
- Active relay direction and deployment ownership.
- Ref/environment guards for secret-bearing workflows.
- Nonzero exit policy for unsolved or invalid evaluation runs.
- Whether Stop remains permanently enabled.

## Current continuation

- I21 (R4-03, verified): `_tg_call` now distinguishes a definite Telegram rejection from an unknown delivery outcome via the falsy `_TG_UNKNOWN` sentinel; every mutation path that previously returned a bare None on timeout/network-reset now signals uncertainty, so duplicate media sends and incorrect quota refunds stop on unknown outcomes while definite failures keep their retry/refund logic. Consumers keyed on None treat the sentinel as delivered by design; sites where the value lives on as a Message (streaming placeholder, both continuation sends, `StatusMessage.start`/`start_action`) normalize it to None so the bubble lifecycle never sees a non-Message. Independent review ran three rounds: round 1 found the placeholder/status leak (Defect A), round 2 found `StatusMessage.start` and the continuation sends (B1/B2), round 3 verified all guards and swept all 97 `_tg_call` call sites for remaining crash-class leaks (none; the suppressed stop-control bubble is a documented accepted tradeoff). New regressions in test_bot_transport/test_draw_versions/test_lumen_status/test_bot_streaming; red re-derived in rounds 1-3.
- I22 (R4-04, verified): daily budgets are now reserved synchronously before the network call instead of checked-then-recorded, which is atomic inside the single asyncio loop. Search side: `_take_search_api_credit` (check+note in one step) with `_release_search_api_credit` in a finally when the call did not spend, and the roll of the quota day inside release so a post-midnight refund cannot eat a new-day credit (review round 1). Draw side: `_reserve_image_neurons`/`_release_image_neurons` provisional pool in lumen_quotas, wired per non-owner attempt in `inline_draw`; the success charge moves the weight to total while the same-step release clears pending, cancellation and failures refund exactly once; per-user counters follow the actor contextvar. Owner is exempt on both pools, matching the pre-existing gate semantics. Independent review: round 1 needs_changes (wiring untested, rollover refund), round 2 fixed_verified with both reds re-derived; the rollover over-block of pending by in-flight requests is documented and accepted. Not verified: live concurrency on the Space (transports are mocked, per repo note).
- I23 (R3-03, verified): `revert_draw_version` now writes versions/redo/prompt/anchor/owner and marks the state dirty immediately after the confirmed photo resend; only the cosmetic old-keyboard strip remains after that await, so a cancellation there can no longer leave a visible restored photo with stale state. Regression: cancellation injected at the strip call, red on HEAD (1 failed in 0.25s), re-derived by the reviewer.
- I24 (R3-06, verified): `revert_draw_version` no longer commits versions/anchor when the resend result is falsy - both definite failure and the `_TG_UNKNOWN` unknown outcome - so the anchor can never point at a message that may not exist. Regression `test_revert_unknown_delivery_does_not_commit_state`; red re-derived by the implementer and independently by the reviewer (1 failed, 22 passed on an external scratch copy); reviewer confirmed success values of _tg_call are always truthy, so the wider guard catches exactly None and _TG_UNKNOWN.
- Active batch: I1-I30 and the final review follow-up are closed with `fixed_verified`, including the documentation/source-language correction cycle. The current snapshot is 111 files and 5,731 elements. No confirmed finding remains open.
- Final whole-repository additive/subtractive independent review: the initial `needs_changes` cycle is fixed; the post-I30 whole-project re-review returned `fixed_verified`.
- Latest gate: `pyflakes bot.py lumen_*.py system_prompt.py tests/*.py` clean; `pytest -q` = 2511 passed, 1 skipped in 54.63s; `git diff --check` clean; inventory validator passed; proxy 49 passed plus check/lint; worker 20 passed; `pip check` and `pip-audit` clean.
- R11 coverage critic completed with repairs; its exposed gaps are recorded as RG findings.
- The audit and all code/test changes remain uncommitted. No push, merge, deploy or live call has occurred.
- Twenty findings are deferred with recorded reasons, including the local eval harness host-access limitation and the proxy DNS-pinning limitation. Protected-file comment translation also remains deferred under the repository's approved-plan rule; all non-protected Python comments/docstrings are normalized.

### R12

| ID | Priority | Status | Location | Finding |
|---|---|---|---|---|
| R12-01 | P1 | verified | `lumen_state_storage.py:262`; `tests/test_bot_state.py:6544` | Snapshot history validation now rejects non-string `content`/`text`, invalid roles and missing text before restore or backup export; the empty-`content` plus non-string legacy-`text` case is covered. |
| R12-02 | P2 | verified | `eval/harness_eval.py:547,637,644`; `tests/test_eval_harness.py` | Touched tests now make an eval run unsolved, and single/batch commands return nonzero for unsolved payloads. |
| R12-03 | P2 | verified | `lumen_search.py:254`; `tests/test_lumen_search.py:1181` | Search-credit rollover now happens before the day comparison, preventing an old-day refund from consuming a new-day credit. |
| R12-04 | P1 | verified | `lumen_transport_calls.py:248,267`; `lumen_commands.py`; transport/command tests | Cancelled Telegram mutations retain cancellation while marking delivery uncertainty by method; draw and TTS no longer refund a possibly accepted send. |
| R12-05 | P3 | verified | `docs/ARCHITECTURE.md:329` | TikTok deliveries are now correctly documented as excluded from the daily answer quota. |
| R12-06 | P3 | verified | `audit/` | The one-off `tmp_refresh.py` ledger writer was removed; only the reproducible validator remains as audit tooling. |

## Remaining verification gaps

- Live streaming failover and `/stop` behavior on the deployed bot.
- Actual GitHub branch protection, environment and secret policy.
- Real relay/Deno deployment topology and transport behavior.
- Container memory limit and measured retained-media peak.
- Backup restore correctness, retention and recovery objectives in production.
- Runtime behavior with real provider credentials and user data.

## Final assessment

The repository materially improved over baseline with 117 independently verified fixes and 20 reasoned deferrals. No confirmed finding remains open. One accepted P1 limitation is confined to the local eval harness and fails closed without explicit acknowledgement. Live-transport verification stays with the owner after merge.

## Final report (mission close-out)

### Initial state
Baseline commit 4b5efc7665d3eed08e4cf11e193efd814762d355: 123 tracked files, 4092 inventoried elements
(AST-enumerated functions, methods, classes, tests, markdown sections, Deno/TS units). The pre-audit
suite ran green; the persona/routing/provider chains were single-process webhook code with no current
budgeting of shared quotas, no unknown-delivery transport contract, and several unguarded display paths.

### Audit coverage
R1-R11 section reviews plus a coverage-critic pass (RG findings) covered every tracked file and element;
the ledger records one file record and one element record each, with per-finding priorities and
decisions (KEEP/IMPROVE/REMOVE/DEFER). Excluded from review: generated caches, `__pycache__`, vendored
lockfiles — standard exclusions, no first-party source skipped.

### Improvements
117 findings verified fixed through red -> fix -> green -> independent review (batches I1-I30): privacy
guards (I1), state/quota integrity and recovery (I2, I12), webhook retry and draw-cancellation ordering
(I19), keyed-provider redirect/admission hardening (I20), unknown-delivery transport contract
`_TG_UNKNOWN` across 97 call sites (I21), atomic synchronous daily-budget pools for search credits and
image neurons (I22), revert-draw state ordering and unknown-send guard (I23-I24), formatting parity and
collision-safe escaped emphasis (I25), UTF-16/Rich activity composition and preview/not-modified
semantics (I26), and agent stop/dedup/budget/tool contracts (I27); plus proxy /read lifecycle and IPv6
classifier fixes, CI-vs-docs truth, eval env scrubbing, dependency-closure and hard-capacity work.
Tests grew from 2005 at baseline to 2511, with each fix carrying focused regression evidence and
review-found defects retained in history. The AST test inventory grew from 1415 to 1706 functions:
295 were added and four baseline tests were replaced by stricter successors (fail-closed quota
loading, dirty draw-state quarantine and stateful leak scanning), not dropped as redundant. No new
runtime dependency was introduced; the approved PyYAML development dependency closes the workflow-test contract.

### Quality verification
Gate at close: pyflakes clean; `pytest -q` = 2511 passed, 1 skipped in 54.47s; `git diff --check` and inventory validation clean.
Focused I25/I26/I27/I29 suites = 175/114/44/260 passed; final TikTok/eval files = 228 passed.
`python -m pip check` reports no broken requirements and `pip-audit -r requirements.txt` reports no
known vulnerabilities. Proxy gate: deno test 49 passed plus deno check/lint clean; worker: 20 passed.
Recorded performance evidence: the independent I25 review measured 4,096 formatting characters at
0.0003 s and 8,000 escapes at 0.0021 s; the two route-timeout tests cost 0.54 s and 0.61 s in
`pytest --durations`; the hung-DNS regression probe completed in 520 ms. The 20,000-user quota case
is a capacity regression, not a claimed throughput benchmark.
`python audit/validate_inventory.py` validates baseline/current file hashes, element hashes, ids,
coverage, findings, deferral reasons and report consistency. Independent reviews include seven
rounds for I25, four for I26, one each for I27-I29, a final needs_changes cycle with fixed_verified
re-review, documentation/source-language review and a final whole-repository additive/subtractive
review. Hygiene: no secrets, user text or live endpoint output are retained in the artifacts.

### Remaining issues
20 findings are deferred with per-finding reasons in the ledger. The two material limitations are
R6-01 (the Deno/ Workers fetch API cannot pin the checked DNS answer to the subsequent connection)
and residual R8-01 host-file access in the local eval harness. R8-01 is confined to developer tooling,
not production runtime, and now requires `HARNESS_UNSAFE_HOST_ACK=1`; safe execution requires a
disposable OS-isolated VM/container without host credentials. Other deferrals concern live-provider
verification, protected-file/owner decisions and low-value migrations. DOC-01 records the protected
Russian comment/docstring residual; no protected file was edited without the required approved plan.
No confirmed finding remains open.

### Final assessment
The codebase is materially safer (transport contract, atomic budgets, cancellation-consistent state,
validated media/moderation boundaries), more honest (docs/CI matched to reality), and better
regression-protected than the baseline, at +506 suite tests and zero new runtime dependencies.
No confirmed finding remains open; 20 deferrals are listed with reasons. Nothing was committed or
deployed, and live Telegram/Space behavior (streaming failover, `/stop`, real concurrency, Docker
build and restore) remains unverified by design — the owner must run the
docs/ARCHITECTURE.md manual smoke checklist after merging the branch.


## Completion audit

| Requirement | Evidence |
|---|---|
| Exhaustive discovery and baseline | 123 tracked files and 4,092 baseline elements; baseline gates recorded above. |
| Complete file/function inventory | `audit/engineering-audit.jsonl`; current snapshot 111 files / 5,731 elements, including decorator and entrypoint metadata on code elements; `audit/validate_inventory.py` is the final close-out gate; the whole-project and final source-language reviews are `fixed_verified`. |
| Individual and contextual review | R1-R11 reviews, coverage-critic follow-up, I1-I30 reviews; zero current `NOT_REVIEWED` elements. |
| Two-way additive/subtractive audit | 18 R10 candidates, one rejected finding, verified removals/deferrals, and a final whole-repository `fixed_verified` re-review after six R12 fixes. |
| Architecture and runtime flows | `docs/ARCHITECTURE.md`, module review records, lifecycle/cancellation/quota/transport fixes. |
| Performance and resource use | Measured formatting and route-timeout timings, a 520 ms hung-DNS probe, 20,000-user capacity regression and bounded media/read streams. |
| Security and reliability | Privacy/state/transport/eval/Docker findings processed; `pip-audit` clean; residual R6-01/R8-01 explicitly deferred. |
| Dependencies | No new runtime dependency; approved PyYAML dev dependency; `pip check` clean. |
| Test audit and regressions | 2,005 baseline to 2,511 passing final tests (1 skipped); 1,415 to 1,706 AST test functions, with 295 additions and four stricter replacements; red/green and mutation evidence; no test weakened merely to pass. |
| Documentation | Canonical technical docs in US English; redundant Russian README removed; non-protected Python comments/docstrings normalized with executable-AST equivalence; protected-file residual explicitly deferred. |
| Multi-agent workflow | Independent review records per significant batch; final additive/subtractive re-review returned `fixed_verified`. |
| Final report and remaining issues | This document, machine-readable ledger, exact counts, deferrals and live-verification gaps. |
| Final prompt-to-artifact audit | `review-COMPLETION-r2` returned `fixed_verified` with no blockers; it rechecked counts, hashes, I26 review history, AST locations and wrong-map rejection. |

