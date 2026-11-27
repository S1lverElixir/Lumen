# Claude reference: what is proven and how

Compiled on 16.11.2026 by four independent agents from archived snapshots of official
documentation and live product pages. Every claim is marked by type:

- `document` — an official Anthropic/Claude page, with a link and snapshot date;
- `observation` — something visible in code, logs, or a live page without an account;
- `inference` — a conclusion from mechanics, with no direct statement. The final parity
  record should contain none of these: either prove them or remove them.

Geographic block: `platform.claude.com` from Belarus redirects to
`anthropic.com/app-unavailable-in-region`. The working path is an archived snapshot:
`https://web.archive.org/web/<timestamp>/https://platform.claude.com/docs/en/<path>`.
The snapshots below were verified to return 200.

## 1. Web search

Sources: `agents-and-tools/tool-use/web-search-tool` @ 20261115132940 and
20260802060611; `build-with-claude/search-results` @ 20261110101637;
`agents-and-tools/tool-use/server-tools` @ 20261114070156.

`document`:

- Tool versions: `web_search_20250305` (base), `web_search_20260209` (plus dynamic
  filtering with code), `web_search_20260318` (plus `response_inclusion`).
- Parameters: `max_uses` (search limit per turn; the default is not stated in the
  documentation; failed searches count), `allowed_domains`/`blocked_domains` (mutually
  exclusive, otherwise 400; subdomains are included, and paths are supported only in
  search), `user_location` (`approximate` plus at least one of city/region/country/timezone),
  `allowed_callers` (on `_20260209`+ it defaults to code execution), `response_inclusion`
  (`full` by default).
- Citations are ALWAYS included: `web_search_result_location` with `url`, `title`,
  `encrypted_index`, and `cited_text` up to 150 characters. Citation fields are not billed.
- Result: `web_search_result` with `url`, `title`, `page_age` (when the site was updated),
  `encrypted_content` (required in the response, otherwise 400).
- Errors arrive inside HTTP 200: `too_many_requests`, `invalid_tool_input`,
  `max_uses_exceeded`, `query_too_long`, `request_too_large`, `unavailable`.
  Empty results mean empty `content`, not an error.
- A long turn can return with `stop_reason: "pause_turn"` and continue with the same
  message and the same tool set.
- Price: $10 per 1,000 searches plus tokens; results in subsequent turns count as input
  tokens; one search equals one unit regardless of the number of results.
- When it searches: current events and news; current prices, rates, balances, and
  statistics; information about organizations, people, and products that may have
  changed; an explicit search request. It does not search for stable facts, mathematics,
  science, or code; creative work; analysis of already supplied material; or greetings.
  It is controlled by the system prompt.
- Volume: “Single-fact queries typically use 1–3 searches; comparative or multientity
  research can use 10 or more”.
- Dynamic filtering (article `claude.com/resources/articles/
  improved-web-search-with-dynamic-filtering`, 17.02.2026): the model writes and runs
  code that filters results before they enter the context. Measurement: +11% average
  accuracy, −24% input tokens. BrowseComp: Sonnet 4.6 33.3→46.6, Opus 4.6 45.3→61.6.
  DeepsearchQA (F1): Sonnet 52.6→59.4, Opus 69.8→77.3.

What this means for Lumen (gaps):

- The cap of 1 search at `medium` and 2 at `high`/`xhigh` is below even a simple factual
  question (1–3). Fixed on 16.11.2026: `medium` 2, `high` 2, `xhigh` 3 with a hard phase
  cap of 45 s; going further (3 at high) requires recalculating the message budget.
- Claude's citations are tied to claims and include the quoted fragment; Lumen has a
  “Sources” block at the end. The form must be decided against the reference while
  accounting for Telegram.
- URL provenance: Claude permits reading only URLs from the person's message, client
  tool results, and previous searches/reads. A URL invented by the model cannot be read.
  This rule is implemented in `read_page`; only URLs already present in approved context are fetched.
- A single source failure must not bring down the entire block: Claude keeps the error
  inside the result, while Lumen's layers are already isolated; verify this with a test.

## 2. Page reading

Source: `agents-and-tools/tool-use/web-fetch-tool` @ 20261114173552;
`tool-combinations` @ 20261109204930.

`document`:

- Versions: `web_fetch_20250910`, `_20260209` (dynamic filtering), `_20260309`
  (cache), `_20260318` (`response_inclusion`).
- `max_uses` — no default cap; failed reads count.
- `max_content_tokens` — text truncation (not for binary PDFs); the limit is approximate.
- `citations` are OFF by default and enabled with `{"enabled": true}`.
- Content types: text, HTML, PDF. JavaScript rendering is not supported.
- Domains: `allowed_domains`/`blocked_domains` are mutually exclusive; only the domain
  is checked for reading, and a path entry does not work.
- URL provenance: a URL that appeared only in model output or only in the system prompt
  cannot be read. The person's message, client tool results, and previous web search/fetch
  are allowed. `url_sources` controls three sources (`user_input`,
  `client_tool_results`, `server_tool_results`), with all set to `all` by default.
- Cost: no surcharge, only tokens. Reference points: a page of ~10 KB ≈ 2,500 tokens;
  documentation of ~100 KB ≈ 25,000; PDF of ~500 KB ≈ 125,000.
- Search integration: “search surfaces candidate URLs; fetch retrieves full page
  content” — first select 2-3 snippets, then read.

What this means for Lumen: the main gap was closed on 16.11.2026. The bot previously saw
only 300-character excerpts and a 2,000-character block; the agent loop now has `read_page`:
a protected `/read` route in the proxy (public https, address checks after DNS resolution,
no redirects, text only, 2 MB cap), text truncation via `READ_PAGE_MAX_CHARS`, the same URL
provenance as Claude, and fallback to snippets on any failure.

## 3. Effort and thinking

Sources: `build-with-claude/effort` @ 20261112131514; `build-with-claude/thinking`
@ 20261113181315; `extended-thinking` @ 20261115091709; `task-budgets` @
20261111140034; support `using-extended-thinking` @ 20250918072953.

`document`:

- Five levels in descending order: `max`, `xhigh`, `high`, `medium`, `low`.
- Default `high` on every model with effort, except Opus 5.5 and Haiku 5.5, which use `medium`.
- `max` — no token limit, and not available on every model; `xhigh` — long tasks
  (30+ minutes), also not available on every model. Not every model with `max` supports `xhigh`.
- Effort affects ALL response tokens: text and explanations, tool calls and their
  arguments, and thinking. “Lower effort also means fewer and terser tool calls”:
  fewer calls, several operations in one, and no preamble.
- Effort does NOT regulate visible response length: “Effort controls thinking volume, not
  visible response length… prompt for length instead” (Opus 5, Fable 5.2).
- “Effort is a behavioral signal, not a strict token budget”.
- Effort is set in `output_config.effort`; `adaptive` is a thinking mode, not a level.
- Manual `thinking: {type: enabled, budget_tokens: N}` on 4.7+ is rejected with 400.
- Effort can be changed mid-conversation: through a new `output_config` (which resets
  the cache) or per-message effort (beta header; the cache is preserved).
- `max_tokens` is a hard cap on all output (thinking plus text); effort is a soft recommendation.
- The person sees a “Thinking” indicator with a timer and an expandable block; clicking
  it shows a summary of the thought process. Raw reasoning is never returned; `display`:
  `summarized`, `omitted` (default for the 5.x lineup), `updates` (beta).
- Practice: `high` is the starting point almost everywhere; `medium` for Opus 5.5/Haiku 5.5;
  `xhigh` for coding and agent tasks; `max` rarely. Settings must not be transferred
  between models; levels are recalibrated.
- The string “Thought for Ns” was not found in the documentation (`observation`): an
  indicator with a timer and an expandable block are proven; the exact wording is an `inference`.

What this means for Lumen (main conclusion): the scale is shifted one step down. Claude
defaults to `high`, while Lumen defaults to `medium`, so the model calls tools less often
and performs one search where Claude would perform three. Change: default `high` for
substantive questions, `medium` for short replies, and `low` for small talk; do not introduce
`max` (on the free heads measured, `xhigh` already depleted the output on a logic problem).
Do not regulate response length through effort — only through the prompt. This matches
the current prompt; verify that the code does not override length through effort.

## 4. Input images and documents

Sources: `build-with-claude/vision` @ 20261110213423; `files` @ 20261114001715;
`pdf-support` @ 20261115194528; support `how-do-usage-and-length-limits-work`.

`document`:

- Input: base64, URL, `file_id`. Formats JPEG/PNG/GIF/WebP; animation is not supported
  (first frame). Up to 8000×8000 px, 10 MB in base64 (10 MB on claude.ai).
- Number of images: 20 per message on claude.ai; 100 per API request with a 200k window,
  600 for other windows; above 20, a stricter dimension limit applies (~3,000 px).
- “Claude has access to every image from earlier turns, so follow-up questions such
  as "Are these similar to the first two?" work without including the earlier images
  again in the new turn”. The model refers to images in words (“the first two”).
- The API is stateless: the client resends history, and images count toward the limit again.
- The model does not receive image metadata.
- Tokens: 28×28 patches; long side up to 1,568 px (~up to 1,568 visual tokens);
  high resolution (4.7+) up to 2,576 px and 4,784 tokens.
- Context compression replaces old turns with a text summary; the fate of images is
  not described in the documentation (`inference`: they remain only in the summary text).
- PDF: up to 600 pages, 128 MB per request, no password; each page is sent as both text
  and an image; 1,500-3,000 tokens per page plus the page cost as an image; documents
  support citations, images do not.

What this means for Lumen:

- Up to two earlier photos within 30 messages currently enter the context. Claude has all
  images in the conversation. This needs review with token arithmetic: retain `file_id`
  for all photos, include more (for example, the latest 4-6), and add more on an explicit
  reference (“that photo,” “the first one”), not only based on age.
- An image in response to “find similar ones” should work without the user sending it again.
- PDF: Lumen sends documents through Gemini; check page/size limits and that text and
  tables are not lost.

## 5. Response interruption

Sources: Claude Code `interactive-mode` @ live; Claude Desktop changelog
v1.32885.1 (18.08.2026) and v4.29.0 (27.10.2026); Claude Tag `how-it-works`.

`document`:

- Claude Code: `Esc` stops the current response or tool call; “Claude keeps
  the work done so far”; the message queue is preserved and sent immediately.
- Claude Desktop: Escape in a panel closes the panel; a second Escape stops the response.
  Fixed: the stopped first response no longer blocks the chat (v4.29.0), and a message
  sent immediately after the stop is not returned to the input field.
- Claude Tag in Slack: the indicator has a Stop button; Claude stops work, preserves the
  session, and writes a line stating who stopped it.
- There is no official article about the Stop button in claude.ai; quota consumption for
  an interrupted response is not described in the documentation (`observation`).

What this means for Lumen: interruption was implemented on 16.11.2026 as the `/stop`
command and the always-on inline Stop button: a per-chat flag, with the streaming loop checking
it before and after each chunk; partial text remains and is stored in history; a localized
notice is added; automatic continuation is not started. The question's author or the owner
can stop it; another person's call is silently ignored; the command is not in the menu,
with the `STOP_COMMAND_ENABLED` switch. Quota consumption for an interrupted response:
the stream does not have time to charge the limit before the stop (the “one update,
one charge” flag is set only on a successful response), and this is intentional: the
person did not receive a complete response. There is no evidence of how Claude handles
this; the reference entry is marked as an observation.

## 6. Claude.ai system prompt

Sources: `release-notes/system-prompts` @ 20260822083035 (Opus 5, 24.07.2026) and
markdown variant @ 20260625112313; support `personalization`.

`document`:

- Markup: XML tags inside one root block, with prose inside. Opus 5 order:
  `product_information`, `fable_safeguards_routing`, `default_stance`,
  `refusal_handling` (inside `critical_child_safety_instructions`),
  `legal_and_financial_advice`, `tone_and_formatting`, `user_wellbeing`,
  `anthropic_reminders`, `evenhandedness`, `responding_to_mistakes_and_criticism`,
  `knowledge_cutoff`; `tone_preference` is outside the root.
- `currentDateTime` is inserted by the template.
- There is NO web search section or citation rules in the prompt (verified with grep
  across all 2026 entries). Instead, `knowledge_cutoff`: if a fact may be outdated,
  Claude says so and directs the person to enable search.
- There are NO rules for responding in the user's language in the 2026 prompt (there were in 2024).
- There are NO emoji clauses in Opus 5 (they were in 4.5-4.8: “does not use emojis unless the
  person asks or their immediately prior message contains one”).
- Tone and format: “keeps responses focused, brief, and concise”; “avoids over-formatting
  with bold emphasis, headers, lists… minimum formatting needed for clarity”;
  “Bullets are at least 1-2 sentences unless the person requests otherwise”;
  “Claude never uses bullet points when declining a task”; “warm tone… assumes the
  person is a capable adult”; the filler words “genuinely/honestly/actually” are banned.
- Prose by default: “responds in prose rather than lists or bullets unless asked”;
  lists only on request or when the content is composite.
- Tables are not mentioned in the prompt at all (neither prescribed nor prohibited).
- Refusals: “defaults to helping”; refusal only for specific serious harm;
  “If Claude finds itself mentally reframing a request to make it appropriate, that
  reframing is the signal to REFUSE”.
- File: “A prompt implying a file is present doesn't mean one is… so Claude checks for
  itself” — matches Lumen's rule.
- Artifacts and tools: there are no separate artifact blocks; the tools section has `tool_search`
  and the order “find the tool first, then ask the person”.

What this means for Lumen (stage 7):

- Lumen uses one flat English block of 137 lines with sections and headings.
  The structure could be changed to tags or short sections, but only with persona
  measurements: the current form scores 50/50 on 25 questions.
- Lumen has explicit CURRENT INFORMATION and RESPONSE LANGUAGE sections that Claude
  does not. For a Telegram bot with 25 languages and an external snippet block, this is
  an intentional difference: keep them, but verify that they do not contradict each
  other or duplicate tool rules.
- Lumen's “emojis are prohibited by default” rule is stricter than Opus 5 and matches
  4.5-4.8. Owner decision: keep it (the current persona benefits from it) and record it
  as an accepted difference, not a gap.
- Prose by default, minimal formatting, lists of 1-2 sentences, no filler, and “assume a
  capable adult” are already in Lumen; compare the wording.
- Lumen has no explicit ban on lists in refusals: add one if measurements show that the
  model lists reasons for refusing.
- The prompt controls response length, not effort: this is already true in Lumen;
  verify the code.

## 7. What remains unverified

- There is no official article about the Stop button in claude.ai; quota consumption for
  an interrupted response is not described.
- The fate of images during context compression is not described in the documentation.
- The default `max_uses` for web_search is not stated in the documentation; per-search
  result limits and timeouts are not described there either.
- The default `max_content_tokens` for web_fetch is not stated (100000 in the example is just an example).
- The exact text “Thought for Ns” is not confirmed by the documentation.
- Current free tiers for Tavily/Brave/Exa and the ability to register from Belarus require
  separate verification before buying a key.
