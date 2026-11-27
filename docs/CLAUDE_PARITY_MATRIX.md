# Claude parity matrix (state as of 16.11.2026)

Each row covers a surface, what Claude does (according to `docs/CLAUDE_REFERENCE.md`,
where every claim is marked with its source), what Lumen does, and the status. Statuses:
`parity`, `accepted difference` (intentional, with a reason), `gap` (not implemented),
and `owner decision`.

| Surface | Claude | Lumen | Status |
|---|---|---|---|
| Search decision | By question class (current events, prices, changed facts, requests); threshold controlled by the prompt | Regex + zone 2, where the model decides + `[[SEARCH]]` marker; corpus: 35/35 search, 0/20 false positives, zone 2: 0 unnecessary searches out of 20 (`eval/RESULTS.md`, 16.11) | parity in results; architecture differs |
| Number of searches | Simple fact: 1-3; comparison: 10+ | medium 2, high 2, xhigh 3, phase 45 s | accepted difference: 10+ does not fit the 90 s budget and free-tier limits |
| Queries | Written by the model; refined after results | Same (`lumen_agent`); refinement 11/12 (measurement, 12.11) | parity |
| Page reading | Full page text and PDF, truncated by tokens, URL provenance | `read_page` through the protected `/read` route (public https, SSRF checks, no redirects, text, 2 MB), same provenance as Claude | parity in code; live run after proxy deployment |
| Citations | Inline, tied to claims, `cited_text` ≤150 characters | “Sources” block at the end, 1-3 links; only URLs actually seen. Inline citations were tried on 16.11: 48/50 and 46/50 versus 50/50 — reverted | gap in form; measurement recorded in `eval/RESULTS.md` |
| Honesty without search | Does not claim it searched when it did not | Same; “does it lie about sources?” probe: 0/10 (13.11) | parity |
| Effort | Five levels, default high (except Opus 5.5/Haiku 5.5); affects tool calls, not response length | Four levels (low..xhigh), default medium for substantive requests; searches tied to the level; length controlled by the prompt | accepted difference: `max` is not introduced; xhigh already depleted output |
| Visible thinking | Indicator with a timer and an expandable block | “✻ Thought for N s,” expandable activity block | parity; the exact Claude wording is not documented |
| Response interruption | Stop: partial text remains and the chat stays usable | `/stop` and the Stop control, always enabled in production: stops from the first second, partial text is stored in history, “continue” works | parity in meaning; live failover still needs the owner's smoke test |
| Stream smoothness | Client-side rendering | Telegram edits: tick 0.55 s, slot 0.3 s (private chat), 1.8 s (group); a short response takes 1-2 frames — a transport limit | accepted difference: Telegram constraints |
| Input images | Sees all images in the conversation | 2 latest photos, 4 when explicitly referenced; every extra image costs tokens and bandwidth | accepted difference based on cost |
| Image generation | No native generation | `/draw`: Cloudflare klein (generation and edits) → Pollinations; budget in Neuron, document above 10 MB | ChatGPT is the reference, not Claude |
| System prompt | XML tags, with no sections on search, language, or emojis | Flat English block with explicit sections on search, language, and the emoji ban; 50/50 on the working corpus (16.11, first token 4.4 s) | accepted difference; rewrite only with measurements |
| Tone and format | Prose by default, minimal formatting, lists of 1-2 sentences, no filler | Same + stricter emoji policy (like 4.5-4.8), “you” by decision on 25.09 | parity in spirit |
| Languages | No language rule in the 2026 prompt | 25 packs; responds in the question's language (25/25 in the 12.11 measurement) | parity in results |
| Tools | web_search, web_fetch, code execution, memory, artifacts | web_search + read_page; no code execution or artifacts | accepted difference: Telegram and the free tier |
| Limits and refusals | Provider quotas, honest errors | Derived limits, honest refusals, daily caps in `/stats` | parity in principle |
| History and memory | Server-side context compression | 200-message log, summary, chat memory; writes protected against loss during failures | accepted difference: custom storage |

## Gaps that remain open

1. Inline citations: decision on the form and persona measurement.
2. Search loop on the Gemini reserve: code is ready (`lumen_agent_gemini.py`, off by default) as of 16.11.2026; a live run on the reserve head is needed.
3. Prompt structure rewrite: not started; only with live before/after measurements.
4. Deep research (10+ searches) does not fit the message budget: phase budgets need recalculation,
   and `MESSAGE_TOTAL_BUDGET_SEC` may need to be raised together with the relay.
5. Live Stop-control smoke test: the control is enabled in production; verify `/stop`, partial-text retention and continuation after deployment.
6. Live post-deployment checks: `/read`, `/stop`, two searches in the logs, and the manual streaming checklist.
