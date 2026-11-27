# Harness-eval results (21.10.2026)

Experiment: does automatically editing `AGENTS.md` help an agent solve tasks
(the method from google-research/harness2, but without the framework itself).

## Conditions

- Agent: OpenCode CLI v2.1.2, model `opencode/glm-5.3-flash-free` (free).
- Editor: `opencode/nemotron-3-ultra-free`. Google models were unavailable from this account
  ("User location is not supported"), so the model from the article
  (Gemini 3.5 Flash) could not be reproduced.
- Tasks: 7 items from the repository history (a fix commit changes code and tests). The sandbox
  starts at `fix^` with tests from `fix`; the target tests (FAIL_TO_PASS) were determined empirically.
- Judge: the repository gate (`pytest -q` + `pyflakes`), as in CI.
- Harness variants:
  - **base** — the current `AGENTS.md`;
  - **weak** — a two-line `AGENTS.md` (control: whether the file has any effect at all);
  - **candidate** — an `AGENTS.md` proposed by the editor based on reports from baseline
    runs: 0 deleted lines, with a "Shell and tooling rules" section added;
  - **fixed** — an `AGENTS.md` with two manual lines about PowerShell and the gate form
    for Windows (the variant ultimately proposed to the owner).

## Summary

| Task | base | candidate | fixed | weak |
|---|---|---|---|---|
| format-newlines-br | solved 311s | solved 675s | solved 329s | solved 155s |
| format-table-separator-bound | solved 337s | solved 469s | solved 265s | solved 192s |
| presplit-glued-bullets | solved 212s | solved 248s | solved 309s | solved 175s |
| state-corrupt-index | solved 262s | solved 512s | solved 242s | solved 282s |
| state-dirty-counters | solved 353s | solved 406s | solved 328s | solved 344s |
| streaming-catchup-throttle | solved 340s | solved 537s | solved 529s | solved 348s |
| tiktok-dns-private-ips | solved 580s | solved 501s | solved 360s | solved 319s |

| Metric | base | candidate | fixed | weak |
|---|---|---|---|---|
| Solved | 7/7 | 7/7 | 7/7 | 7/7 |
| Edited tests | 0 | 0 | 0 | 0 |
| Tasks with Unix commands | 5 | 1 | 0 | 4 |
| Ran pyflakes | 7/7 | 7/7 | 7/7 | 0/7 |
| Ran pytest | 7/7 | 7/7 | 7/7 | 7/7 |
| Total time | 2395s | 3348s | 2362s | 1814s |
| Total shell commands | 77 | 76 | 67 | 48 |

## What this means

1. **No ceiling.** All three harnesses solve 7/7. On tasks like these, "fix a bug with
   failing tests," the content of `AGENTS.md` does not change the outcome — the model and
   the tests themselves do the work. Automatically editing the rules provides no gain here.
2. **The difference shows only in discipline.** The weak harness does not know about pyflakes and
   never ran it — the gate is formally "green" on tests, but incomplete.
   The full `AGENTS.md` always runs the gate.
3. **Automatic edits solve what they can see, but at a cost.** Unix commands in PowerShell
   (`pytest ... | tail -30`, where `tail` does not exist) dropped from 5 tasks to 1, but
   total time rose by 40% (3348s versus 2395s): there are more rules, and the context
   costs more.
4. **Automatic edits without a validator create a contradiction.** The editor added `python`, `pip`, and `python -m` to the "never use" list — but the gate requires
   `python -m pytest`. The rule overrides the gate. In harness2, validators
   in the framework handle this (they have their own tree checks and protection against leaks into the rubric); my
   simplified version has none.
5. **Two manual lines beat automatic edits.** The `fixed` variant (two lines about PowerShell
   and the gate form for Windows) produced 0 Unix commands, 67 shell commands instead of 77, and a time of
   2362s versus 2395s for the baseline — that is, less noise and faster, with a complete gate.
   Automatic edits in the same place produced 1 Unix command and 40% more time.
6. **The remaining Unix command in candidate is telling.** The agent ran the gate as
   `bash -lc 'pyflakes bot.py lumen_*.py ...'`: that is, it wrapped the command in bash to
   get Linux globs. The reason is in `AGENTS.md` itself: the gate line is written in
   CI form (Linux), while the host is Windows, PowerShell.

## Incidental issues found and fixed

A full test run on Windows ended with noise in stderr: "Unclosed client
session" from aiohttp and "--- Logging error ---" from a logger that tried to
format a record while the interpreter was already exiting. The pytest exit code in
this case was 0 — the noise was misleading and changed me (and the agent: it spent 2-3 commands each time
analyzing a supposedly "failed" run).

Causes and fixes:

- `tests/test_bot_commands.py`: unit tests invoked the bot's real `aiohttp.ClientSession`.
  An autofixture was added to replace `_get_http_session` with a stub.
- (historical note; `_fish_audio_tts_bytes`, `FISH_AUDIO_ENABLED`, and OpenRouter were removed on 05.11.2026)
- `lumen_commands._fish_audio_tts_bytes`: the session opened before the key was checked
  for OpenRouter. Now, if the key is empty, the function returns None immediately, without a session (in
  the product, the branch was already disabled by FISH_AUDIO_ENABLED; the fix removes noise from
  unit tests and an unnecessary session in general).

After the fixes: 999 tests passed, stderr was empty, exit code 0.

## Conclusion

The Harness² approach ("the agent fixes its own harness") does not pay off on this repository:
the ceiling was reached without any automatic edits, while the edits themselves have a side effect
(a contradiction with the gate) and increase the time cost.

What is worth keeping:

- two manual lines in `AGENTS.md` about PowerShell and the gate form for Windows —
  a measurable gain: 0 Unix commands instead of 5, fewer shell commands, and the gate was
  unaffected;
- "evidence-backed lessons" (the second recursion of harness2) — cheap, without a framework:
  after a failure, write a rule into the file with a reference to that failure;
- the measurement tool `eval/` itself — it can be used to check any future change to `AGENTS.md`.

## Limitations

- One run per task and per variant: variance was not measured; the conclusions are qualitative.
- Tasks of a single type (fixing a bug from failing tests) — harness rules have
  almost no effect on them; on other types (refactoring, releases, prompt edits) the effect
  may differ, but that is already a different experiment.
- MCP servers were disabled in the sandbox: runs were faster and network-free, but this is not exactly
  the agent's usual environment.

---

# Agent search-loop measurement (12.11.2026)

Experiment: does the `lumen_agent.run_search_turn` loop bring in more fresh
sources than the previous single external search on the person's raw phrase, and what does it
cost in time and tokens? The tool is `eval/agent_loop_probe.py`, the head is
`deepseek-ai/deepseek-v4.1-flash-1017`, the host is global, and the key belongs to the owner.

## Conditions

- 12 real, recent questions covering different intents (trainer, rate, ticket, model release,
  weather, exchange rate, match, package version, TV series, executive, hardware choice,
  country news).
- `effort` was selected automatically for each question (`medium` — that is the form of these
  questions), and the search cap derived from effort = 1.
- The production system text was used (`get_system_prompt`); history and past photos were disabled:
  the measurement covers the loop, not context assembly.

## Summary

| Metric | Value |
|---|---|
| loop invoked search | 11 out of 12 |
| sources found | 11 out of 12 (3-5 per question) |
| average phase time | 3.12 s (range 2.5-4.6 s, search skipped in 0.59 s) |
| provider requests | 12, errors 0, no 429s |
| tokens | in 69,549, out 1,370, of which reasoning 523; ~5.9K tokens per planning call |
| searches > 1 | 0 — with a cap of 1, refinement is physically impossible |

The model wrote the query formulations itself, and they were better than the original phrase: "current
Central Bank of Russia key rate today," "Minsk to Moscow train ticket price 2026," "weather in
Gomel tomorrow, November 13, 2026" (it inserted the year and date from context). One
question ("who currently leads NVIDIA") was ignored by the loop, which would have answered from
memory — that is the price of giving the model the right to choose.

## Limitations

- One run of these twelve questions; it was repeated twice more that evening,
  and the search count remained 11 out of 12 — but these are three samples with n=12, not a measured
  time variance.
- With `effort=medium`, the search cap = 1, so this run says nothing about
  refinement; "does the model refine its query after seeing the results" was measured that evening with the same probe
  at a cap of 2 — see the "Cap of two searches" section below.
- The planning call in the probe runs without chat history; the production path will add to its
  cost, as will past photos (disabled with `attach_past=False`) — the real cost is higher.
- Sources are counted as found, not sufficient: "does the block contain the answer"
  is measured by a different probe, `eval/search_parity_probe.py` (10/10 on 12.11.2026).

## Effort grid: automatic selection versus human labels (12.11.2026)

`eval/effort_grid.py` — 46 formulations with manually assigned levels. This is a deterministic
yardstick, like `search_trigger_corpus`: no judge is needed, because the rule is being checked,
not the model's taste. The plan's threshold: at least 80% matches and zero misses of
"thinks less than needed."

| Run | Matches | Under-effort |
|---|---|---|
| signal "question mark + digit + at least three words" | 40/46 = 87% | 2: "who are the Vikings" and "why is the sky blue" fell to low |
| signal "question words in 25 languages + requests for brevity not only in RU + words and length" | 46/46 = 100% | 0 |

The label in the grid was wrong, not the code (the same case in the tests was also wrong): "solve it again, you're wrong,
recheck the calculation" is a request to think MORE CAREFULLY (one level up, high), but the label
was medium. The old label resulted from the previous rule "shorter than forty characters —
that means small talk," which dropped the baseline to low and thereby hid the increase.

What the grid does NOT measure:

- Answer correctness. The relationship between level and facts rests on a single arithmetic
  measurement (glm-5.3-flash: 64 at low versus 60 correct at high, 12.11.2026); on the remaining
  questions, the labels represent agreement with the rule, not proven accuracy.
- Spelling of lists that are absent from the bot's corpus: of 67 non-Latin
  question-word forms, 45 do not appear in the bot's own strings, so there is no local way to
  verify them; an error can only add a level (a short reply
  will move from low to medium, not the reverse).
- Scripts without spaces, except Thai: Hindi and Bengali are covered by words,
  but a question written without spaces and without a question mark will not be detected by the signal.

## Repeat run after the effort signal was edited (12.11.2026, evening)

The same probe, the same 12 questions, the same search cap (medium = 1), but now with the new
question signal in `pick_reasoning_effort`.

| Metric | Value |
|---|---|
| loop invoked search | 11 out of 12 (same as in the morning) |
| sources found | 11 out of 12 |
| average phase time | 4.36 s (3.12 s in the morning); median 4.66 s, p95 across 12 measurements 4.94 s, worst 5.13 s — the "p95 within 40 s" target has an eightfold margin |
| provider requests | 12, errors 0 |
| planning call | 0.73 s, in 69,549 / out 1,350 / thinking 439 tokens, ~5.9K per request |

The time increase is unrelated to the edit: in this run, DuckDuckGo lite, DuckDuckGo
html, and Wikidata all raised TimeoutError, and the block was assembled from Bing News and Google News
(5 snippets each instead of dated Wikidata and DuckDuckGo rows). The signal edit does not touch either the freshness route or
the search cap — it only changes low/medium on short replies without question
words, and all 12 questions in this probe were already medium.


## Repeat of the deterministic measurements on the evening of 12.11.2026

Both ran after the effort signal was edited and after the documents were edited — so the plan's thresholds
(§6.1, §6.2) were based on the current tree, not the morning snapshot.

| Measurement | Result | What this means |
|---|---|---|
| `eval/search_trigger_corpus.py` | should search 35 out of 35, misses 0; should not search 20 out of 20, false positives 0 | the freshness detector did not shift in either direction after the effort edit; the second panel (20 conversational formulations) is the text-level check for "not lying about search": not one short reply invokes search or receives a "without live search" label |
| `eval/search_parity_probe.py` | 10 out of 10, phase 4.0-7.5 s per question | the assembled block contains the required answer token; the tenth case ("Ugoona Abdulmanova") must be empty, and it is empty |

A caveat about speed: in this run, DuckDuckGo lite, DuckDuckGo html, and Wikidata raised
TimeoutError on every question, and the block was assembled from Bing News, Bing Web, and Google News.
The baseline measurement on 11.11.2026 gave 4 s instead of the previous 7 s (the log entry in docs/PROMPT_EVAL.md);
today they are slower, and this is the state of the network, not a code regression.

What the §6.4 threshold ("the bot does not lie about search") actually covers: the corpus above (no
false invocations), the "without live search" label in the code path, and — since 13.11.2026 — a separate
probe, `eval/no_search_honesty_probe.py`, which listens specifically to the answer text
(0 claims about sources out of 10, see the section below).


## Munito capability map: live run on 12.11.2026

Before this hour, the claim that "gemma-4-12b and pplx-decider-v1.2 return 400 on
`reasoning_effort`, while text heads accept it" rested on one-off calls that
were not in the repository: `eval/munito_probe.py` checked neither `tools` nor `reasoning_effort`,
and the `probe-tools` run from 05.11 sampled other models and on a different
provider. `eval/provider_caps_probe.py` was added — 10 requests, one per pair
(model, parameter), max_tokens=8, with a key from the main tree; it never prints the values.

| Model | `reasoning_effort` | `tools` |
|---|---|---|
| deepseek-ai/deepseek-v4.1-flash-1017 | accepted (200, 0.46 s) | accepted (200, 0.32 s) |
| zai-org/glm-5.3-flash | accepted (200, 0.44 s) | accepted (200, 0.35 s) |
| deepseek-ai/deepseek-v4-flash | accepted (200, 0.27 s) | accepted (200, 0.35 s) |
| google/gemma-4-12b | 400 "'reasoning_effort' is not supported" (0.16 s) | accepted (200, 0.73 s) |
| perplexity-ai/pplx-decider-v1.2 | 400 (0.15 s) | 400 (0.14 s) |

The failure is an `invalid_request_error` with a `param`, not a timeout or a 5xx, so
`_MUNITO_CAPS` may silently drop the parameter for these two models rather than
blame the network. A 400 for "the wrong parameter" arrives in 0.14-0.16 s —
that is, the test in `tests/test_bot_routes.py`, which emulates it with a fake 400,
checks the code's behavior, not a provider fact: the fact now comes from this run.

## What a live persona run showed (12.11.2026, 25 questions)

Method: `eval/persona_models.py run --models munito:deepseek-v4.1-flash-1017
--judge munito:zai-org/glm-5.3-flash`, production system prompt, no chat history
added, automatic effort selection enabled (of 25 questions, 21 went to medium, 4 to
low). The figures come from the run log, which also gives 50/50.

| Metric | Value |
|---|---|
| first token | median 0.80 s, average 1.3 s according to the tool's final line |
| entire answer | median 4.00 s, average 5.4 s, worst 17.9 s |
| tokens per request | 5,461-5,880 (median 5,658), two outliers at 12.1-12.4K — questions where the model requested search and the snippet block arrived in the prompt |

This is the cost of the level: on the production prompt, medium adds single-digit seconds to the answer,
not tens. A separate measurement, "effort only, same prompt," was made on 13.11.2026 with
`MUNITO_REASONING_EFFORT=low` pinned: 49/50 versus 50/50 with automatic selection. A one-point difference —
is within the noise of a single run, so "46/50 became 50/50 thanks to effort" remains
unproven, only a suspicion: the 12.11 run included both the new level and the system prompt clause
about the tool from 01d44ec.

## Does the bot lie about sources when there was no search (13.11.2026)

Item §6.4 of the plan ("does not lie about search") previously relied only on the corpus
`eval/search_trigger_corpus.py`, which checks whether search was invoked, not the answer text.
`eval/no_search_honesty_probe.py` was added: ten recent questions, with the same label attached to the production
system prompt that the code adds when search is empty
(imported from `lumen_message_core`, not copied); the answer is checked for words claiming
sources and for a nearby negation.

| Run | Result |
|---|---|
| 10 questions (Central Bank rate, trainer, bitcoin, weather, news, Fable 5.2, chancellor, euro exchange rate, rentals, tickets) | **0 claims about sources out of 10** |
| First run of the same probe | 1 "claim" — false: the answer "I cannot confirm the exact rate — I do not have current data right now" was classified as a source listing because the negation appeared in the opposite order. The classifier was fixed; the answers are unchanged |

A typical honest answer from the run: "I cannot check live prices for tomorrow —
I do not have access to ticketing systems or aggregators right now, so the figures below
are approximate." That is, the "without live search" label does its job: the model
presents figures as estimates, not as findings.

## A/B of effort on the persona: one point, that is, within noise (13.11.2026)

The open question was this: can yesterday's 46/50 → 50/50 be attributed to the new
effort level. A separate measurement was made with a pin: `MUNITO_REASONING_EFFORT=low`
(flat behavior before the branch) versus automatic selection, with the same system prompt,
the same 25 questions, and the same judge.

| Run | Score | First token | Answer |
|---|---|---|---|
| effort pinned to `low` | **49/50** | 1.8 s | 6.7 s |
| automatic selection (21 questions out of 25 at medium) | **50/50** | 1.3 s | 5.4 s |

Conclusion: on this grid, the rubric is already saturated (49-50 out of 50 in both arms), and one
point of difference proves nothing — the effort difference here is below the judge's resolution.
The figure is useful in another way: it rules out claiming "50/50 thanks to effort," and
the earlier entry was corrected for this. Effort remains justified not by the persona, but by
another measurement — glm-5.3-flash on arithmetic (64 at low versus 60 correct at high).

## Cost of the effort signal itself (12.11.2026)

Method: `time.perf_counter` over 20,000 calls to `pick_reasoning_effort`, with a mandatory warmup;
module import was measured in a separate process.

| What | Value |
|---|---|
| importing `lumen_router_config` | 26-50 ms (first import in a process, ~40 ms on a warm tree) |
| one call on a short reply | 4.4 μs ("hello"), 21-35 μs on a reply containing signal words |
| 20,000 characters in one word after "software" | 11-13 ms |
| 200,000 characters | 103-119 ms |

Growth is linear (20K/40K/80K characters → 16/32/63 ms), with no catastrophic
backtracking in the regexes: all alternatives are literals, and the long-range window
is narrowed to 25 characters within a phrase.

## First night in production: the planning call hit the cap (13.11.2026)

The branch was deployed to Space at 14:23, and in the first six hours in production there were five planning
calls. There was no other way to see the loop at a real prompt size: in the probe
the transcript is short, and planning cost 0.73 s:

| Time | Chat | planning_ms | Outcome |
|---|---|---|---|
| 14:29:01 | #1a3c4b4f | 2680 | steps 1, searches 1, snippets 5 |
| 14:29:54 | #1a3c4b4f | 6351 | the model decided not to search (an honest refusal, steps=0) |
| 14:32:43 | #1a3c4b4f | 8433 | `planning call failed: MunitoAPIError`, search did not happen |
| 14:57:43 | #1a3c4b4f | 8723 | same thing, and the fallback external search also returned zero |
| 15:30:26 | #b571760e | 2000 | steps 1, searches 2, snippets 10 — query refinement in production |

Two failures out of five are exactly the previous single-call cap (8 s): planning with
Munito on a 10,000-token prompt with reasoning does not always fit within it. The fallback
worked as designed (the answer went through the normal route, the model was not
quarantined, and there were no exceptions or crashes in the log), but the person was denied
the right to "search like Claude." The single-call cap is now derived from the phase,
rather than being a separate number: the phase remainder minus one external search,
but no more than 16 s; if there is not enough time for both the call and the network,
the call is not made at all. The measurements from the 12.11.2026 run (median 4.66 s, p95 4.94 s) do not
invalidate this: they describe the entire phase with a short transcript.

## Cap of two searches: does the model refine its query (12.11.2026, evening)

The open question from the previous run: with `medium`, the cap was one search, and there was no way
to check "does the model refine its query after seeing the results." The run was repeated with
`PROBE_EFFORT=high` (cap = 2), with the same 12 questions and the same head.

| Metric | medium, cap 1 | high, cap 2 |
|---|---|---|
| loop invoked search | 11 out of 12 | 11 out of 12 |
| questions with two searches | 0 | **11 out of 12** |
| sources per question | 3-5 | 9-10 |
| average phase time | 4.36 s (the network was failing in that run) | 3.37 s |
| provider requests | 12 | 13 |
| tokens per request | ~5.9K | ~5.97K |

The conclusion that was previously a guess: the model does write a second, semantically
different query instead of repeating the first. This is visible verbatim in the live log: "Central Bank of Russia key rate decision October 2026" followed by "Bank of Russia key rate
today." The second search happens within the same step, so the phase time did not increase;
the extra cost is one planning request per question.


# Offline search checker after the parity audit (16.11.2026)

Command: `python eval/search_offline_check.py` (32 anonymized cases from the weekly corpus,
without network access or keys).

- Intent misses: 0. Regex false positives: 2: id 8 "a panoramic sunset over the sea" and
  id 11 "Today is an ordinary day" — both were documented in advance in
  `docs/ARCHITECTURE.md` as cases left for the model to decide within the loop; fixing them with a regex
  would mean trying to capture semantics with a word list.
- 10 cases are closed by the agent loop (the regex is silent, the model decides), wiki expectations 4,
  and not one of them contains the word "wiki" in the text — this class is covered by the loop instruction
  to add `wiki` for game entities, not by the deterministic layer.
- The deterministic layer was not changed by this audit; only the search cap changed
  (`medium` 2 instead of 1) and the phase to 45 s.

The trigger corpus (`python eval/search_trigger_corpus.py`, that same evening): should search 35,
misses 0; should not search 20, false positives 0. That is, the search cap was raised without a regression in
detection: 35/35 and 0/20 were preserved.


# Persona measurement before the prompt edits (16.11.2026, evening)

Conditions: `munito:deepseek-ai/deepseek-v4.1-flash-1017`, judge `munito:zai-org/glm-5.3-flash`,
a subset of 8 questions from the main corpus (8 "I got fired," 11 "iPhone or Samsung,"
13 comparing python/go, 16 sorting, 18 "talk like a pirate," 20 checking the owner,
22 "$1,000," 24 capital of australia), maximum 2 points per question.

Result: **16/16, judge errors 0**, first token 1.7 s, answer 7.9 s.

Conclusion: on this subset, the prompt is already at the ceiling. Three candidates from the structural review
(prohibiting lists in a refusal, minimum list item length, the phrase "an adult person")
were NOT added: there is no measurable gain, and there is a risk of regression on the full corpus —
the rule "change the prompt only with a measurement" worked in the opposite direction. The full corpus
of 25 questions was not run in this session because of the quota; on the next prompt change,
compare against this baseline on the same 8 questions and on the full corpus.

The full corpus that same evening (25 questions, the same head and judge, no prompt edits):
**48/50**, the output showed no judge errors, first token 2.6 s, answer 7.9 s. The difference from
the previous 50/50 (12.11, a different run) is within the judge's stated error of ±2 and is not
explained by the edits: the prompt had not changed since that measurement. The per-question run file was not
saved (the `--out` directory remained empty), so the two lost points could not be localized;
on the next prompt change, compare against both figures (16/16 on the subset
and 48/50 on the full corpus).

## Repeat of the offline search probes, 16.11.2026 (evening, branch agent/full-audit-claude)

`python eval/search_offline_check.py`: 32 cases, regex false positives 2
(cases 8 and 11), model-decided cases 10 (covered by the agent loop), intent misses 0.
Wiki expectations: 4 (13, 14, 26, 32), with no occurrence of the wiki word in the text — covered by
the loop instruction.

`python eval/search_trigger_corpus.py`: should search 35 out of 35, should not search 0 out of 20.

The figures match the 16.11 daytime entry: that day's edits (the evidence frame, provenance,
deadlines) did not move the decisive layer.
`python eval/search_parity_probe.py` that evening: 10/10 cases, blocks of 1.0-1.9 KB,
about 4.0 s per case. An important environment detail: DuckDuckGo lite and html returned
TimeoutError on ALL requests in the run, while the answers were sustained by Bing (news/web RSS) and Google
News. This matches the 12.11 measurement ("DDG lite returned an anomaly on 8 out of 8"): the
layer must not consider DDG available, and the ranking already accounts for this.
An important note about model names: by the evening of 16.11, the short id `munito:deepseek-v4.1-flash-1017`
from the 12.11 entry returned 404, while the full `munito:deepseek-ai/deepseek-v4.1-flash-1017`
worked (checked with one question: 2/2, first token 2.4 s). Old method lines
do not need to be rewritten, but new runs should use only the full id.
## Zone 2 on the live loop, 16.11.2026 (evening)

The completeness critic correctly noted: the 35/20 corpus measures the regex, while "the model decides"
was not measured. The new probe `eval/zone2_probe.py` runs the same 20 "do not search"
formulations through a real planning call (Munito, medium effort, a cap of
1 search), using a key from the main checkout's `.env`.

Result: questions 20, **extra searches 0**, planning calls 20, errors 0.
That is, Zone 2 does not search small talk, math, recipes, or code even when the regex
is silent; the cost is one short planning call for such a message (5970-6100
input tokens, 200-300 ms).
## Full persona run on the current prompt, 16.11.2026 (evening)

Method: `eval/persona_models.py run --models munito:deepseek-ai/deepseek-v4.1-flash-1017
--judge munito:zai-org/glm-5.3-flash`, full corpus of 25 questions, production system
prompt, automatic effort selection. Run `20261116-181544`.

Result: **50/50, judge errors 0**. Speed: first token averaged 4.4 s,
answer 9.9 s. Questions 6 and 22 requested search and went through a second pass with snippets
(12.1-12.8K tokens) — that is the expected cost of fresh facts.

This resolves the completeness critic's question about the fact that the 50/50 in the matrix was dated 12.11:
on the current prompt, the figure was confirmed again, without prompt edits.
## Other probes from stage 8, 16.11.2026 (evening)

- `python eval/effort_grid.py`: 59/59 (100%), misses of "thinks less than needed" — 0.
- `python eval/no_search_honesty_probe.py` (live, Munito): claims about sources without
  search — 0 out of 10; all answers honestly state that live data is unavailable.
- `python eval/provider_caps_probe.py` (10 live calls): the map matched the recorded one —
  deepseek v4.1/v4 and glm-5.3-flash with tools and reasoning_effort; gemma-4-12b without
  reasoning_effort; pplx-decider-v1.2 without tools and without reasoning_effort.
- `eval/image_probe.py` and `eval/munito_probe.py` were not rerun that evening:
  the image layer and provider limits did not change that day, and generation spends
  the owner's daily Neurons; previous dated figures remain valid.
- `eval/agent_loop_probe.py` was not rerun: the loop's behavior was measured by the live
  Zone 2 probe and the persona run (questions 6 and 22 produced a second pass with snippets).
## Citations next to claims: live prompt measurement, 16.11.2026 (evening)

Prompt edit: place the source as a link next to the claim itself (as Claude does), while the
"Sources" block remains only when there are more than two distinct sources. Full corpus,
same head and judge: **48/50** (judge errors 0, first token 3.2 s, answer 8.1 s)
versus 50/50 on the prompt before the edit. A 2-point difference lies exactly at the judge's stated
error of ±2, so a second full run was performed; the decision on the edit
is based on two runs, not one.

The format works: the bitcoin exchange-rate answer contains two links next to claims
(CoinGecko, MEXC), with no "Sources" block added; answers without search contain no links.
The second run of the same edit: **46/50** (judge errors 0). Across two runs — 48 and 46
versus 50/50 on the previous prompt — the edit placing citations next to claims does not pass the rule
"change the prompt only with a measurement" and was rolled back (a rollback commit that same evening).
The link format itself worked (two links next to claims in the exchange-rate answer),
but the overall persona score falls, and that matters more than footnote formatting: the "Sources" block at the end
remains, and the difference from Claude in format is recorded in the matrix as open.
