"""lumen_message_core.py is the incoming message processing core: albums, passive
group background, rate limiting, attachment parsing, and _handle_message_core.
Album buffers live in bot.py; communication with it uses deferred imports inside
functions.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from aiogram.enums import ChatType, ParseMode
from aiogram.types import Message, Update

from lumen_message_parse import (
    DRAW_TRIGGER_PREFIXES,
    EDIT_TRIGGER_IMPLICIT,
    EDIT_TRIGGER_PREFIXES,
    REDO_TRIGGER_PREFIXES,
    REPEAT_TRIGGER_PREFIXES,
    REVERT_TRIGGER_PREFIXES,
    TTS_TRIGGER_PREFIXES,
    _detect_image_variants,
    _is_bare_trigger_request,
    _is_new_artifact_request,
    _match_draw_intent,
    _has_visual_signal,
    _is_anaphoric_draw_request,
    _match_trigger_with_fillers,
    _draw_trigger_is_visual,
    _tts_trigger_is_speech,
    _split_image_variants,
    _strip_variants_phrase,
    _wants_style_reference,
    _NO_MEDIA_NOTE,
    _find_recent_media_by_category,
    _media_reference_category,
    _strip_reply_marker,
    extract_url,
    is_tiktok,
    is_youtube,
    match_pick_request,
)
from lumen_media import (
    _ensure_prompt_text,
    _media_file_id_and_mime,
    _msg_media_source,
    _youtube_summary_prompt,
)
from lumen_agent import evidence_trace
from lumen_router_config import (
    _build_route,
    _effort_auto_on,
    _looks_like_freshness_query,
    _looks_like_heavy_query,
    _looks_like_search_request,
    agent_max_searches,
    agent_searches_capped_by_env,
    pick_reasoning_effort,
    recent_corrections,
    search_planner_skipped,
)
from lumen_search import (
    _SearchRequested,
    search_daily_budget_left,
    answered_with_live_search,
    clean_publisher,
    fetch_web_snippets,
    format_snippets_for_prompt,
    route_has_live_search,

    is_anaphoric_followup,
    web_search_is_enabled,
    web_search_max_results,
    web_search_timeout_sec,
)
from lumen_security import _looks_like_injection_probe
from lumen_status import StatusMessage, discard_status_start
from lumen_rich import NO_LINK_PREVIEW
import lumen_devtrace
import lumen_memory

log = logging.getLogger("bot")

# The date for freshness questions is calculated inside the call; Moscow is the default for the RU audience:
# Insert a hard line break after the prefix: %A/%B depend on the system locale:
# setlocale is never called (locale C on both HF and tests).
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _fresh_date_context() -> str:
    """A one-line current-date context for fresh-question prompts."""
    now = datetime.now(_MOSCOW_TZ)
    return f"[Context: today is {now.strftime('%d %B %Y')}, {now.strftime('%A')} (Europe/Moscow).]"


# The model requested a search ([[SEARCH]]), but there is nothing to search with or for; without this line
# the second pass would return the same marker, and the user would get no
# answer at all.
_SEARCH_UNAVAILABLE_NOTE = (
    "\n\n[No web search is available for this turn. Answer from what you already know, "
    "in the user's language, and say in one plain line what you could not verify. "
    "Do not ask for a search and do not refuse to answer. "
    "This bracket is an instruction to you, not text to show: never quote, translate or "
    "reproduce it, and never mix its language into the reply.]"
)


def _media_prompt_lang(message: Any, state: dict | None) -> str | None:
    """Select the saved locale, then the Telegram client language.
    Return None when neither is supported; /lang itself is handled outside this media path."""
    saved = (state or {}).get("lang")
    if saved:
        return saved
    code = getattr(getattr(message, "from_user", None), "language_code", None)
    if not code:
        return None
    # normalize_lang would return en even for unsupported sv-SE, imposing
    # English; we are checking the code base itself.
    from lumen_lang import LANG_NAMES
    base = str(code).strip().lower().replace("_", "-").split("-")[0]
    return base if base in LANG_NAMES else None


def _previous_user_text(state: dict) -> str:
    """The last question from the same chat: a short follow-up (\"and who came before her?\")
    is searched by the previous question's topic, not by the pronoun (measurement 11.11.2026)."""
    for entry in reversed(state.get("history") or []):
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        text = (entry.get("content") or entry.get("text") or "").strip()
        if len(text) >= 12:
            return text[:300]
    return ""


# Bare time words: this is a reaction reply, not a question about fresh data.
_BARE_TIME_WORDS = {"вчера", "сегодня", "завтра", "now", "today", "tomorrow", "yesterday"}


_SELF_QUESTION_RE = re.compile(
    r"(?i)^\s*(?:кто|что)\s+(?:ты|вы)(?![А-Яа-яA-Za-z])"
    r"|^\s*ты\s+кто(?![А-Яа-яA-Za-z])"
    # "Who am I" is a question about the person and chat memory; the web has nothing to do with it (prod 17.11.2026).
    r"|^\s*кто\s+я(?![А-Яа-яA-Za-z])"
    r"|что\s+ты\s+(?:знаешь|помнишь)\s+обо\s+мне"
    r"|как\s+меня\s+зовут"
    r"|who\s+am\s+i|what\s+do\s+you\s+know\s+about\s+me"
    r"|как\s+(?:тебя|вас)\s+зовут"
    r"|что\s+(?:ты|вы)\s+(?:умеешь|можешь|знаешь)"
    r"|who\s+are\s+you|what\s+are\s+you(?![A-Za-z])|what\s+can\s+you\s+do"
)


_SOCIAL_WORDS = frozenset({"привет", "здравствуй", "здравствуйте", "добрый", "доброе", "хай", "hello", "hi", "hey", "спасибо", "thanks"})


def _wants_web_search(text: str, *, previous: str = "") -> bool:
    """Whether the bot should search the way Claude would: fresh data is needed, or searching was explicitly requested. A reference to a past conversation (\"remember, we discussed this yesterday\") is a question to chat memory; searching that memory was unnecessary (measured 11.11.2026).

previous is the preceding question: \"who was before her\" has no topic of its own, but it continues a fresh question, and Claude searches for that one."""
    if not text:
        return False
    if _PAST_CHAT_RE.search(text):
        return False
    # A greeting/thanks is no reason to search: "lumen hi" after a short
    # from the previous reply went to Tavily (prod 18.11.2026).
    _first = text.strip().split()[0].strip(",.!?—–-").lower() if text.strip() else ""
    if _first in _SOCIAL_WORDS:
        return False
    # A question about the bot itself needs no search: "Who are you?" after any reply
    # was treated as a topic continuation and burned Tavily (prod 17.11.2026).
    if _SELF_QUESTION_RE.search(text):
        return False
    # A bare time word — a reactive remark ("yesterday," "today" in response to "when?"),
    # not a question: the freshness regex catches it alone (week corpus 15.11.2026).
    # Normalize to letters only: emoji, parentheses, and NBSP do not make “yesterday” a question.
    if re.sub(r"[\W_]+", "", text, flags=re.IGNORECASE).lower() in _BARE_TIME_WORDS:
        return False
    if _looks_like_freshness_query(text) or _looks_like_search_request(text):
        return True
    return bool(previous) and is_anaphoric_followup(text)


def _munito_head(route: list[tuple[str, str]]) -> str | None:
    """The route's first Munito model that supports tools: the planning call
    is needed only for it (Gemini has its own server-side grounding tools)."""
    from lumen_router_config import _munito_model_supports

    for provider, model_id in route:
        if provider != "munito":
            continue
        return model_id if _munito_model_supports(model_id, "tools") else None
    return None


def _snippet_fetch_budget_sec(deadline: float | None) -> float:
    """Budget for one external search: it has its own timeout, but no more than the remaining route budget, or, without that, the remaining message budget (the previous calculation)."""
    import bot

    budget = web_search_timeout_sec()
    if deadline is not None:
        return min(budget, max(0.0, deadline - time.monotonic()))
    remaining = bot._msg_remaining_sec()
    if remaining is not None:
        budget = min(budget, remaining)
    return budget


async def _gather_web_evidence(
    *, chat_id: int, user_prompt: str, clean_prompt: str, previous: str,
    route: list[tuple[str, str]], effort: str | None, status, deadline: float | None,
    history_meta: dict | None, allow_fallback_fetch: bool = True, zone2: bool = False,
) -> tuple[list[dict], list[str], bool, dict]:
    """Fresh sources for one message: the agent loop, with the previous
single external search based on the person's words beneath it.

The loop provides what the classifier cannot: the model writes the queries, sees
the results and refines the wording (measured 12.11.2026: for 'is he definitely not
in the club?' the head produces two separate queries instead of the raw phrase). An empty or
aborted loop must not deprive the answer of search; the old branch still runs.
In zone 2 (the regex stayed silent, the model decides), no fallback fetch by raw
phrase is made: it searches for small talk, and a model refusal is a normal outcome
(prod 15.11.2026: 'theoneprobe' without zone 2 answered from memory); the loop needs
at least one slot, otherwise deciding whether to search is impossible in principle.
Returns (snippets, model queries, whether the loop ran, loop metrics)."""
    import bot
    import lumen_agent
    from lumen_router_config import agent_max_searches

    if not web_search_is_enabled():
        return [], [], False, {}
    queries: list[str] = []
    # The search cap is derived from effort; EFFORT_AUTO=0 gives effort=None, and then
    # the loop stays completely silent — exactly the previous single search by the user’s words.
    max_searches = agent_max_searches(effort)
    if zone2 and effort is not None and max_searches < 1 and not agent_searches_capped_by_env():
        # The decision "search or not" requires the call itself: low-effort must not
        # mute zone 2 to zero (prod 15.11.2026: “theoneprobe” was answered from memory).
        # EFFORT_AUTO=0 (effort=None) disables the entire loop — the existing invariant,
        # and the owner's AGENT_MAX_SEARCHES=0 flora limit bypass (review 16.11.2026).
        max_searches = 1
    if search_daily_budget_left() <= 0:
        # Daily request ceiling reached: the loop will not find anything anyway,
        # but can still spend model planning calls (up to three per message).
        # Silently return the old branch — it will honestly return emptiness and a mark
        # "without live search" (privacy review 12.11.2026).
        max_searches = 0
    head = _munito_head(route) if max_searches > 0 else None
    # the route deadline is not set here yet (it is calculated before routing),
    # the phase gets its own budget within the message budget; otherwise
    # planning calls had no bound at all. Compute it once for both cycles.
    phase_deadline = None
    if max_searches > 0:
        phase_deadline = deadline if deadline is not None else bot._deadline_within_message(
            lumen_agent.evidence_phase_budget_sec(max_searches)
        )
    if head is not None and lumen_agent.agent_turn_enabled():
        turn = None
        try:
            turn = await lumen_agent.run_search_turn(
                chat_id=chat_id, user_prompt=user_prompt, clean_prompt=clean_prompt,
                model_id=head, effort=effort, max_searches=max_searches, status=status,
                deadline=phase_deadline, history_meta=history_meta,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[agent] search turn crashed, falling back to a plain fetch: %s", exc.__class__.__name__)
        stats: dict = {}
        if turn is not None:
            queries = list(turn.queries)
            stats = {"steps": turn.steps, "planning_ms": turn.planning_ms,
                     "searches": turn.searches, "failed": turn.failed,
                     "pages": list(getattr(turn, "pages", []) or []),
                     "reads": int(getattr(turn, "reads", 0) or 0)}
            if turn.has_evidence:
                return turn.snippets, queries, True, stats

    # Gemini fallback: the same loop if the Munito head is unavailable (absent from the route,
    # disabled or selected a cap) and the loop is allowed by a Space variable.
    from lumen_agent_gemini import gemini_agent_enabled, run_search_turn as _gemini_search_turn
    _gemini_head = next((m for p, m in route if p == "gemini"), None)
    if gemini_agent_enabled() and _gemini_head is not None and head is None and max_searches > 0:
        try:
            _gturn = await _gemini_search_turn(
                chat_id=chat_id, user_prompt=user_prompt, clean_prompt=clean_prompt,
                model_id=_gemini_head, effort=effort, max_searches=max_searches,
                status=status, deadline=phase_deadline, history_meta=history_meta,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[agent-gemini] turn crashed, falling back to a plain fetch: %s", exc.__class__.__name__)
            _gturn = None
        if _gturn is not None:
            _gstats = {"steps": _gturn.steps, "planning_ms": _gturn.planning_ms,
                       "searches": _gturn.searches, "failed": _gturn.failed,
                       "pages": [], "reads": 0}
            if _gturn.has_evidence:
                return _gturn.snippets, list(_gturn.queries), True, _gstats

    budget = _snippet_fetch_budget_sec(deadline)
    snippets: list[dict] = []
    if budget > 0 and allow_fallback_fetch and not bot._stream_stop_requested(chat_id):
        try:
            snippets = await fetch_web_snippets(
                clean_prompt, timeout_sec=budget, max_results=web_search_max_results(),
                context=previous,
            )
        except Exception as exc:
            log.warning("[web-search] Snippet fetch failed, answering with a no-search note: %s", exc.__class__.__name__)
    return snippets, queries, False, {}


def _initial_status(needs_youtube: bool, needs_website: bool, media_mime: str | None,
                    search_will_run: bool) -> tuple[str, bool]:
    """The first phrase in the status bubble: show the stage that actually runs first."""
    if needs_youtube:
        return "progress_watching_video", False
    if needs_website:
        return "progress_reading_link", False
    if search_will_run:
        return "progress_searching", False
    if media_mime:
        if media_mime.startswith("image/"):
            return "progress_viewing_image", False
        if media_mime.startswith("video/"):
            return "progress_watching_video", False
        # Audio and documents are not video: previously, a voice note showed
        # "Watching video", and PDF is the same thing (review 28.10.2026).
        return "progress_thinking", True
    return "progress_thinking", True

# Total cap on extra album files: 9 files of 20MB + base64 gave ~250MB
# in RAM (audit D1, 30.09.2026). A regular album is tens of MB.
# Continuing a truncated answer makes sense only if the message budget
# at least that many seconds remain for one route pass.
_CONTINUE_MIN_BUDGET_SEC = 12.0

_ALBUM_EXTRA_MAX_BYTES = 100 * 1024 * 1024
# Concurrent album downloads: limits in-flight memory (3×20MB).
_ALBUM_FETCH_CONCURRENCY = 3

async def _run_under_chat_lock(
    message: Message, run: Callable[[], Any], *, background: bool = False,
) -> None:
    """Single entry into processing under a per-chat lock: the message time limit, a chat label in
the log, a cap on simultaneous waiters and an honest 'busy' refusal. Ordinary messages,
albums and guest updates otherwise differ in these protections: background work and albums
were not counted as waiters, and guests had no message time limit at all
(audit 01.11.2026).

background is passive recording of group background: it does not reply, so
it does not take a place in the people queue and exits silently if the lock is held (prod 18.11.2026)."""
    import bot
    chat_id = message.chat.id if message.chat else 0
    # Do not reset the deadline or chat marker if already set (the normal path and
    # album task inherits them from handle_message); otherwise the message time limit
    # would shift by the time spent waiting for the lock.
    dtoken = None if bot._msg_remaining_sec() is not None else bot._MSG_DEADLINE.set(
        time.monotonic() + bot.MESSAGE_TOTAL_BUDGET_SEC
    )
    ltoken = None if bot._LOG_CHAT.get() else bot._LOG_CHAT.set(f"chat={chat_id}")
    # Albums and pick buttons arrive via a background task/callback, bypassing handle_message:
    # without a scope here, /stop does not work during the search phase on these paths (audit 16.11.2026).
    _stop_scope = chat_id if message.chat is not None else None
    # Author for model quarantine: pick-button callbacks were previously counted
    # with a chat, not a person (audit 16.11.2026).
    utoken = None
    _sender = getattr(message, "from_user", None)
    if not bot._LOG_USER.get() and _sender is not None:
        utoken = bot._LOG_USER.set(f"user={getattr(_sender, 'id', 0)}")
    # Actor for per-user search limits: background calls inherit the task context.
    import lumen_search as _lumen_search
    _actor_token = _lumen_search.set_search_actor(getattr(_sender, "id", None))
    try:
        remaining = bot._msg_remaining_sec()
        lock_timeout = bot.CHAT_LOCK_TIMEOUT_SEC if remaining is None else max(1.0, min(bot.CHAT_LOCK_TIMEOUT_SEC, remaining))
        # Passive background work does not occupy people's queue slots: otherwise three replies in
        # busy chats filled the cap, and an addressed question was rejected with "busy",
        # which also went to the wrong recipient (production 18.11.2026).
        stop_scope_opened = False
        counters = bot._chat_lock_background_waiters if background else bot._chat_lock_waiters
        cap = bot._MAX_CHAT_LOCK_BACKGROUND_WAITERS if background else bot._MAX_CHAT_LOCK_WAITERS
        waiters = counters.get(chat_id, 0)
        if waiters >= cap:
            if background:
                log.info("[lock] Background record dropped, chat %s is full", chat_id)
                return
            log.warning("[lock] Too many waiters for chat %s", chat_id)
            with contextlib.suppress(Exception):
                await bot._tg_call(bot._answer_sender(message), bot._t_no_create(chat_id, "lock_busy"))
            return
        # Response without creating a chat: ordinary _t through get_state would create a record even
        # the person rejected with "busy" (the same reason as the limit).
        counters[chat_id] = waiters + 1
        try:
            lock = await bot.acquire_chat_lock(chat_id, lock_timeout)
        except asyncio.TimeoutError:
            if background:
                log.info("[lock] Background record skipped, chat %s is busy", chat_id)
                return
            log.warning("[lock] Timeout waiting for lock on chat %s", chat_id)
            with contextlib.suppress(Exception):
                await bot._tg_call(bot._answer_sender(message), bot._t_no_create(chat_id, "lock_busy"))
            return
        finally:
            left = counters.get(chat_id, 1) - 1
            if left > 0:
                counters[chat_id] = left
            else:
                counters.pop(chat_id, None)
        # The Stop area is created only after acquiring the lock: the queue does not share
        # one event between messages, while /stop works during search.
        if _stop_scope is not None and not background:
            bot._begin_stream_stop_scope(
                _stop_scope, getattr(getattr(message, "from_user", None), "id", None))
            stop_scope_opened = True
        try:
            await run()
        finally:
            with contextlib.suppress(Exception):
                lock.release()
    finally:
        if dtoken is not None:
            bot._MSG_DEADLINE.reset(dtoken)
        if ltoken is not None:
            bot._LOG_CHAT.reset(ltoken)
        if utoken is not None:
            bot._LOG_USER.reset(utoken)
        _lumen_search.reset_search_actor(_actor_token)
        if stop_scope_opened:
            bot._end_stream_stop_scope(_stop_scope)


async def _process_media_group_buffers(mgid: str) -> None:
    import bot
    messages: list = []
    try:
        await asyncio.sleep(0.8)
    finally:
        # Cancellation during sleep still requires the write; otherwise it hangs forever. Another (new)
        # a task with the same mgid; leave it alone and verify it is ours. Buffers
        # only the owner can remove those too: otherwise a cancelled task took them
        # of the new run (review 17.11.2026).
        current_task = bot._mg_tasks.get(mgid)
        if current_task is None or current_task is asyncio.current_task():
            # No owner (direct call/old path), or the owner is us: remove it.
            # Another live task under the same mgid retains its buffer.
            messages = bot._mg_buffers.pop(mgid, []) or []
            if current_task is asyncio.current_task():
                bot._mg_tasks.pop(mgid, None)
    if not messages:
         return
    # An album uses the same per-chat lock and safeguards as a regular
    # messages (see _run_under_chat_lock). A passive group album is silent background work.
    main_msg = next((m for m in messages if m.text or m.caption), messages[0])
    await _run_under_chat_lock(
        main_msg, lambda: _process_media_group_buffers_locked(messages),
        background=_album_passes_to_background(main_msg),
    )

def _album_passes_to_background(main_msg: Any) -> bool:
    """A group album without addressing the bot is passive background, like a single
message."""
    import bot
    if main_msg.chat is None or main_msg.chat.type == ChatType.PRIVATE:
        return False
    return bot._should_only_record_passively(
        main_msg, main_msg.text or main_msg.caption or "",
        is_private=False,
        is_guest=bot.is_guest_message(main_msg),
        mentioned=(
            bot.message_mentions_bot(main_msg)
            or bot.group_wake_request(main_msg.text or main_msg.caption or "")[0]
        ),
    )

async def _process_media_group_buffers_locked(messages: list) -> None:
    """Album processing body under a per-chat lock (see above)."""
    import bot
    # The main message is the one with a caption (the caption may not be on the first photo); otherwise the prompt was lost.
    main_msg = next((m for m in messages if m.text or m.caption), messages[0])
    # A group album without a bot mention is the same passive background as an ordinary
    # message: previously it still consumed a quota slot and downloaded files (hostile
    # 27.09.2026 review). In private chat and when directly addressed, an album is
    #     processed as before.
    if _album_passes_to_background(main_msg):
        t_text = main_msg.text or main_msg.caption or ""
        # Files 2..10 of a passive album also enter the general log: otherwise
        # "what is in the second photo" will not find them (order matches the album).
        _passive_state = bot.get_state(main_msg.chat.id)
        await bot._record_passive_group_context(
            main_msg, _passive_state, t_text,
            extra_sources=[_msg_media_source(m) for m in messages[1:]],
        )
        return
    # The limit is BEFORE downloading: previously the entire album loaded even for a rejected
    # author (audit 26.09.2026). There is exactly one slot: handle_message buffers the album
    # entirely and does not enter _handle_message_core.
    if await bot._reject_rate_limited_message(main_msg):
        return
    # Daily limits — before downloading extras: a global-quota rejection still
    # downloaded nine files (audit 26.10.2026).
    uid = bot._user_key_for_message(main_msg)
    entry = bot._user_daily_peek(uid)
    if await _refuse_daily_total(main_msg, uid, entry):
        return
    # A video slide makes the entire album Gemini-only: without this check, extras
    # would download until the Gemini quota was exhausted.
    if any(_direct_attachment_needs_gemini(m) for m in messages) and await _refuse_daily_gemini(main_msg, uid, entry):
        return
    extra_media: list[tuple[bytes, str]] = []
    album_state = bot.get_state(main_msg.chat.id)
    album_user_id = main_msg.from_user.id if main_msg.from_user else None
    targets = _album_extra_sources(messages, main_msg)
    # Download in parallel rather than sequentially; otherwise a 9-photo album could wait ~10-20 s for consecutive downloads.
    fetched_list: list = [None] * len(targets)
    # The semaphore caps in-flight memory, while the overall cap limits accumulated memory (audit D1, 30.09.2026).
    _album_fetch_semaphore = asyncio.Semaphore(_ALBUM_FETCH_CONCURRENCY)

    async def _fetch_one_bounded(fid: str, mime: str) -> tuple[bytes, str] | None:
        async with _album_fetch_semaphore:
            return await bot._fetch_media(fid, mime)

    try:
        fetched_list = list(await asyncio.gather(
            *(_fetch_one_bounded(fid, mime) for fid, mime, _ in targets),
            return_exceptions=True,
        ))
    except Exception:
        pass
    skipped = 0
    too_big = 0
    extra_bytes = 0
    kept_sources: list = []
    for (fid, mime, src), fetched in zip(targets, fetched_list):
        if isinstance(fetched, bot._MediaTooLargeError):
            too_big += 1
            continue
        if not fetched or isinstance(fetched, BaseException):
            skipped += 1
            continue
        if extra_bytes + len(fetched[0]) > _ALBUM_EXTRA_MAX_BYTES:
            skipped += 1
            continue
        extra_bytes += len(fetched[0])
        extra_media.append(fetched)
        kept_sources.append(src)
        # Album files go into recent_media_ids, otherwise later questions will not find them.
        bot._save_media_to_history(src, album_state, album_user_id)
    if skipped:
        # Failed slides were silently dropped, and analysis ran on only part of the
        # files.
        log.warning("[album] Skipped %d of %d files: download failed, analysing the rest.", skipped, len(targets))
    if too_big:
        # Large files are rejected by the cap: the honest refusal to the user comes
        # from _handle_message_core.
        log.warning("[album] Skipped %d of %d files: over the download size cap.", too_big, len(targets))
    if skipped or too_big:
        # Honest marker: analysis covers only part of the album (review 17.11.2026).
        with contextlib.suppress(Exception):
            await bot._safe_reply(main_msg, bot._t(main_msg.chat.id, "album_partial"))
    # The rate limit was already charged in _process_media_group_buffers_locked: charging twice
    # an album must not take a slot (audit 28.10.2026).
    await bot._handle_message_core(
        main_msg, extra_media=extra_media or None, skip_rate_limit=True,
        # Downloaded slides only: history must not reference media
        # which the model had not seen (review 17.11.2026).
        extra_media_sources=kept_sources or None,
    )


# Send the main message separately, followed by up to 9 additional messages (10 total — the TikTok slideshow limit).
_MAX_ALBUM_EXTRA = 9


def _album_extra_sources(messages: list, main_msg: Any) -> list[tuple[str, str, Any]]:
    """Album files other than the main one: the caption may not be on the first
photo (audit 27.10.2026)."""
    sources: list[tuple[str, str, Any]] = []
    for m in messages:
        if m is main_msg:
            continue
        if len(sources) >= _MAX_ALBUM_EXTRA:
            break
        src = _msg_media_source(m)
        if not src:
            continue
        fid, mime, _ = _media_file_id_and_mime(src)
        if not fid:
            continue
        sources.append((fid, mime, src))
    return sources


_PASSIVE_LINE_MAX_CHARS = 500


def _message_ts(message: Any) -> float:
    """Wall-clock message time: for memory and a pending request before a photo."""
    date = getattr(message, "date", None)
    try:
        return float(date.timestamp())
    except Exception:
        return time.time()


def _queue_passive_history(history: list[dict[str, Any]], state: dict[str, Any]) -> None:
    """Queue passive overflow for a later active summary without calling a provider."""
    from lumen_chat_state import HISTORY_SUMMARIZE_KEEP, MAX_MEMORY_PENDING, MAX_TOTAL_MEMORY_MESSAGES

    if len(history) <= MAX_TOTAL_MEMORY_MESSAGES:
        return
    pending = state.get("memory_pending")
    pending = list(pending) if isinstance(pending, list) else []
    state["memory_pending"] = (pending + history[:-HISTORY_SUMMARIZE_KEEP])[-MAX_MEMORY_PENDING:]
    del history[:-HISTORY_SUMMARIZE_KEEP]


async def _record_passive_group_context(
    message: Message, state: dict[str, Any], t: str, extra_sources: list[Any] | None = None,
) -> None:
    """Group background without a mention: a shared-log reply and media, with no response."""
    import bot
    sender = message.from_user
    display = bot._note_sender(
        state,
        sender.id if sender is not None else None,
        getattr(sender, "username", None) if sender is not None else None,
        getattr(sender, "first_name", None) if sender is not None else None,
    )
    display = _flatten_untrusted(display, 80)
    text_line = _flatten_untrusted(t, _PASSIVE_LINE_MAX_CHARS)
    refs = []
    for source in [_msg_media_source(message), *(extra_sources or [])]:
        ref = lumen_memory.media_ref(source)
        if ref:
            refs.append(ref)
    if text_line.strip() or refs:
        lumen_memory.append_user(
            state, text_line, sender=display,
            sender_id=sender.id if sender is not None else None,
            ts=_message_ts(message), media=refs, addressed=False,
        )
        _queue_passive_history(state["history"], state)
    # The sender’s basket remains a fallback for old chats and type links.
    _uid = sender.id if sender is not None else None
    bot._save_media_to_history(_msg_media_source(message), state, _uid)
    for source in extra_sources or []:
        bot._save_media_to_history(source, state, _uid)
    bot.mark_state_dirty(message.chat.id)


# How much reply text to carry to the model: enough for a quote without bloating the chat.
_REPLY_TARGET_MAX_CHARS = 1000


def _flatten_untrusted(text: str, limit: int = _REPLY_TARGET_MAX_CHARS) -> str:
    """Untrusted text in the prompt: one line, with bracket markers that cannot be forged. Natural-language injections remain constrained by the system prompt and output filters, but structural breakout through ] and a newline is blocked."""
    clean = re.sub(r"[\r\n\t]+", " ", str(text or ""))
    clean = clean.replace("[", "(").replace("]", ")")
    return clean.strip()[:limit]


def _reply_target_block(message: Message, state: dict[str, Any]) -> str:
    """Quote from a reply message: a reply to the bot answers about that message, not the latest one."""
    import bot
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return ""
    text = _flatten_untrusted(getattr(reply, "text", None) or getattr(reply, "caption", None) or "")
    if not text:
        return ""
    ruser = getattr(reply, "from_user", None)
    try:
        bot_username = (bot.BOT_USERNAME or "").lower()
    except Exception:
        bot_username = ""
    r_username = getattr(ruser, "username", None) if ruser is not None else None
    if r_username and bot_username and str(r_username).lower() == bot_username:
        return f"[Отвечаешь именно на это сообщение (реплай) — твой прошлый ответ: {text}]\n"
    if ruser is not None:
        display = bot._note_sender(
            state, getattr(ruser, "id", None),
            getattr(ruser, "username", None), getattr(ruser, "first_name", None),
        )
    else:
        display = "User"
    display = _flatten_untrusted(display, 80)
    return f"[Отвечаешь именно на это сообщение (реплай) от {display}: {text}]\n"


def _tagged_prompt(state: dict[str, Any], message: Message, clean_prompt: str) -> str:
    """A question with an author and a reply: the model knows who is asking and what is being answered."""
    import bot
    sender = message.from_user
    display = _flatten_untrusted(bot._note_sender(
        state,
        sender.id if sender is not None else None,
        getattr(sender, "username", None) if sender is not None else None,
        getattr(sender, "first_name", None) if sender is not None else None,
    ), 80)
    tagged = f"[{display}] {clean_prompt}" if (clean_prompt or "").strip() else f"[{display}]"
    return _reply_target_block(message, state) + tagged + _nick_hints(state, clean_prompt)


# How many nickname hints we carry to the model: enough for the participants without inflating noise.
_NICK_HINT_MAX = 3
# Do not resolve short words: two-letter matches create noise in every message.
_NICK_HINT_MIN_LEN = 3


def _nick_hints(state: dict[str, Any], clean_prompt: str) -> str:
    """Links nicknames in a question to their authors (“pancake” is @user (Name))."""
    import bot
    try:
        words = re.findall(r"[\w@]+", clean_prompt or "", re.UNICODE)
    except Exception:
        return ""
    seen: set[str] = set()
    hints: list[str] = []
    for word in words:
        surface = word.strip().lstrip("@")
        if len(surface) < _NICK_HINT_MIN_LEN or surface.lower() in seen:
            continue
        seen.add(surface.lower())
        resolved = _flatten_untrusted(bot._resolve_nick(state, surface) or "", 80)
        surface = _flatten_untrusted(surface, 40)
        if resolved and resolved not in hints:
            hints.append(f"[Подсказка: «{surface}» — это {resolved}.]")
        if len(hints) >= _NICK_HINT_MAX:
            break
    return ("\n" + "\n".join(hints)) if hints else ""


def _should_only_record_passively(message: Message, t: str, *, is_private: bool, is_guest: bool, mentioned: bool) -> bool:
    """True if the message is group-chat background without addressing the bot (see
    _record_passive_group_context above) and no active processing is needed at all.
    The only exception is a TikTok link, which is ALWAYS processed, even without
    mentioning the bot (historically intentional; see the comment in the original
    _handle_message_core)."""
    if is_private or is_guest or mentioned:
        return False
    if getattr(message, "_pick_resolved", False):
        # Choosing a button is already a direct reply to the bot: in a group, without this check it
        # went into passive background and the user received no answer (audit 27.10.2026).
        return False
    url = extract_url(t)
    if url and is_tiktok(url):
        import bot
        # A group TikTok is downloaded by default even without being addressed (sharing convenience);
        # TIKTOK_GROUP_REQUIRE_MENTION=1 does not restrict passive group audio to mentions
        #     (hypothesis H2, 10.2026).
        return bool(bot.TIKTOK_GROUP_REQUIRE_MENTION)
    # We reach here only when there is no TikTok link — so this is background.
    return True


def _rate_limit_key_for_message(message: Message) -> int:
    """Rate-limit key: without from_user (a channel post), use sender_chat/chat.id; otherwise paid-provider limits could be bypassed."""
    if message.from_user:
        return message.from_user.id
    sender_chat = getattr(message, "sender_chat", None)
    return sender_chat.id if sender_chat else message.chat.id


# A reference to a past conversation, not to the external world: such phrases must not trigger a request
# into search even with the words "yesterday/earlier" (measurement 11.11.2026).
_PAST_CHAT_RE = re.compile(
    r"\b(?:помнишь|помните|помнится)\b"
    r"|\bмы\s+(?:вчера|раньше|ранее|позавчера|в\s+прошлый\s+раз)\s+(?:обсуждали|говорили|сказали|рассматривали)"
    r"|\bкак\s+(?:вчера|раньше|в\s+прошлый\s+раз)\s+(?:говорили|обсуждали|решили)"
    r"|\bты\s+(?:вчера|раньше|earlier)\s+(?:говорил|сказал|упоминал)"
    r"|\bdo\s+you\s+remember\b|\bwe\s+(?:talked|discussed|spoke)\s+(?:yesterday|before|earlier)",
    re.IGNORECASE,
)

_CONTINUE_RE = re.compile(
    r"^(продолжи(ть)?|продолжай|дальше|договори|continue)\s*,?\s*(пожалуйста)?\s*[.!…]*$",
    re.IGNORECASE,
)

_CONTINUE_INSTRUCTION = (
    "\n\n[Продолжи оборванный ответ с места обрыва, не повторяя уже сказанное. / "
    "Continue the interrupted answer from the cut point without repeating what was already said.]"
)

def _continue_after_interrupt(state: dict, clean_prompt: str) -> str | None:
    """\"continue\" after an interrupted stream: the original question plus a note
    to resume from the cutoff. The question-answer pair is searched from the end:
    background group messages may have been appended to the common log after
    the answer, so hist[-2:] is no longer necessarily the pair."""
    if not _CONTINUE_RE.match((clean_prompt or "").strip()):
        return None
    if not state.get("interrupted"):
        return None
    hist = state.get("history") or []
    for idx in range(len(hist) - 1, 0, -1):
        answer = hist[idx]
        question = hist[idx - 1]
        if not isinstance(answer, dict) or answer.get("role") != "assistant":
            continue
        if not (answer.get("content") or "").strip():
            continue
        if not isinstance(question, dict) or question.get("role") != "user":
            continue
        if question.get("addressed") is False:
            continue
        if not (question.get("content") or "").strip():
            continue
        return question["content"].strip() + _CONTINUE_INSTRUCTION
    return None


async def _refuse_daily_total(message: Message, uid: int | None, entry: dict | None, *, force: bool = False) -> bool:
    """Rejection due to the shared daily limit; True means the message was already handled with a rejection."""
    import bot
    if not force and not bot._user_daily_total_exhausted(uid, entry):
        return False
    hours, mins = bot._user_daily_reset_in()
    # Daily /stats counter: a limit refusal means there will be no model response.
    bot._record_stats_event("daily_limit_denials")
    # The refusal must be visible in logs: prod 11.11.2026, a chat received a refusal due to
    # the limit, and there was not a single line in Space logs, so "the bot is silent" was
    # indistinguishable from "the bot refused due to quota". Counters only: no id
    # and no text.
    log.info("[quota] daily message limit reached (used=%s, limit=%s, reset in %dh%dm) — refused before the model",
             (entry or {}).get("total", 0), bot._user_daily_limit(uid, "total", entry), hours, mins)
    await bot._safe_reply(message, bot._t(
        message.chat.id, "user_daily_total",
        used=(entry or {}).get("total", 0), limit=bot._user_daily_limit(uid, "total", entry),
        hours=hours, mins=mins,
    ))
    return True


async def _refuse_daily_gemini(message: Message, uid: int | None, entry: dict | None) -> bool:
    """Refusal due to Gemini's daily quota; True means the message has already been refused."""
    import bot
    if not bot._user_daily_gemini_exhausted(uid, entry):
        return False
    bot._record_stats_event("daily_limit_denials")
    log.info("[quota] daily heavy-model limit reached (used=%s, limit=%s) — refused before the model",
             (entry or {}).get("gemini", 0), bot._user_daily_limit(uid, "gemini", entry))
    await bot._safe_reply(message, bot._t(
        message.chat.id, "user_daily_gemini",
        used=(entry or {}).get("gemini", 0), limit=bot._user_daily_limit(uid, "gemini", entry),
    ))
    return True


async def _reject_rate_limited_message(message: Message) -> bool:
    import bot
    if not bot._check_and_register_rate_limit(bot._rate_limit_key_for_message(message)):
        return False
    # Response without creating a chat: ordinary _t through _chat_lang would create a record even
    # the rejected message. The language of an existing chat is picked up the same way.
    # The log line is the same as for the two refusals above: without it, a refusal based on the 5/30s window looks
    # in production as the bot going silent.
    log.info("[rate-limit] replied with the flood notice (window 5 req / 30 s)")
    await bot._tg_call(bot._answer_sender(message), bot._t_no_create(message.chat.id, "rate_limited"))
    return True


async def _resolve_incoming_media(
    message: Message, state: dict[str, Any], clean_prompt: str, *, is_private: bool,
) -> tuple[str, tuple[bytes, str] | None]:
    """Whose media: 1) attachment, 2) reply, 3) words. The attachment is read directly into
bytes (without a temporary file); the rest goes through _fetch_media in memory."""
    import bot
    asking_user_id = message.from_user.id if message.from_user else None
    media_src = _msg_media_source(message)
    med_name = ""
    media_tuple = None

    if media_src:
        res = await bot._download_message_attachment(media_src)
        if res:
            media_bytes, media_mime, med_name = res
            bot._save_media_to_history(media_src, state, asking_user_id)
            media_tuple = (media_bytes, media_mime)

    # Priority #2: an explicit reply to a media message is the most reliable signal,
    # the user explicitly named the file. Works without trigger words.
    if media_tuple is None and message.reply_to_message is not None:
        reply_src = _msg_media_source(message.reply_to_message)
        if reply_src:
            reply_fid, reply_mime, _ = _media_file_id_and_mime(reply_src)
            if reply_fid:
                fetched = await bot._fetch_media(reply_fid, reply_mime)
                if fetched:
                    # Media from a reply also enters history: a verbal reference must find the file.
                    bot._save_media_to_history(reply_src, state, asking_user_id)
                    media_tuple = fetched

    # Search #3 among the author's media, not the last item in the chat: the
    #     18.08.2026 calibration showed that "show the sticker" referred to someone
    #     else's photo.
    category = _media_reference_category(clean_prompt)
    if media_tuple is None and category:
        # The shared log comes first: it remembers 200 messages with file_id; the small
        #     basket below is a reserve for old chats.
        found = lumen_memory.last_media_for_user(
            state, category, sender_id=asking_user_id, private=is_private,
        )
        if found:
            _entry, ref = found
            fetched = await bot._fetch_media(ref["file_id"], ref["mime"])
            if fetched:
                media_tuple = fetched
    if media_tuple is None and state.get("recent_media_ids") and category:
        buckets: dict[str, Any] = state["recent_media_ids"]
        own_bucket = buckets.get(str(asking_user_id)) if asking_user_id is not None else None
        fid_mime = None
        if own_bucket:
            fid_mime = _find_recent_media_by_category(own_bucket, category)
        elif is_private:
            # In a private chat with the bot there is exactly one interlocutor, so it is less critical,
            # search across every known bucket for the chat at all.
            for bucket in buckets.values():
                fid_mime = _find_recent_media_by_category(bucket, category)
                if fid_mime:
                    break
        if fid_mime:
            fid, mime = fid_mime
            fetched = await bot._fetch_media(fid, mime)
            if fetched:
                media_tuple = fetched

    return med_name, media_tuple


def _strip_trigger_content(
    clean_prompt: str, trigger: str, message: Message, *, reply_fallback: bool = True,
) -> str:
    """Text after a draw/tts trigger: prefix cleanup plus a fallback to the replied-to text
    ("draw this" in response to a message with a description). The fallback is disabled for
    repetitions: "another version" replying to a text answer means text, not an image with
    that caption (review 10.11.2026).
    """
    content = re.sub(r'^[:\s\-\,]+', '', clean_prompt[len(trigger):].strip()).strip()
    content = _strip_reply_marker(content)
    if reply_fallback and not content and message.reply_to_message is not None:
        reply_text = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
        if reply_text:
            content = reply_text
    return content


def _direct_attachment_needs_gemini(message: Message) -> bool:
    """Video/audio/document (not an image) in an attachment or reply is too late
to hand only to Gemini; downloading it in advance is useless when the quota is exhausted."""
    for msg in (message, getattr(message, "reply_to_message", None)):
        if msg is None:
            continue
        # The attribute name already identifies the category without downloading (and without relying on
        # the class name — tests contain doubles; aiogram has its own types).
        for attr in ("video", "video_note", "animation", "voice", "audio"):
            if getattr(msg, attr, None):
                return True
        src = _msg_media_source(msg)
        if src is None:
            continue
        if type(src).__name__ in ("Video", "VideoNote", "Animation", "Voice", "Audio"):
            return True
        _, mime, _ = _media_file_id_and_mime(src)
        m = (mime or "").lower()
        if m.startswith(("video/", "audio/")):
            return True
        if type(src).__name__ == "Document" and not m.startswith("image/"):
            return True
    return False


def _draw_budget_seconds(remaining: float | None) -> float:
    """/draw budget: 0.0 means the remaining budget is exhausted, not \"no value\". Previously `or`
    replaced zero with the full budget, so drawing could exceed the cap (review 17.11.2026)."""
    import bot
    if remaining is None:
        remaining = bot.DRAW_TOTAL_BUDGET_SEC
    return max(1.0, min(bot.DRAW_TOTAL_BUDGET_SEC, remaining))


async def _handle_message_core(
    message: Message, extra_media: list[tuple[bytes, str]] | None = None, *,
    skip_rate_limit: bool = False, extra_media_sources: list[Any] | None = None,
) -> None:
    import bot
    t = message.text or message.caption or ""
    is_private = message.chat.type == ChatType.PRIVATE
    is_guest = bot.is_guest_message(message)
    mentioned = bot.message_mentions_bot(message)
    # Wake word ("Lumen, ...") in groups: forwarded messages and third-person discussions
    # do not count it as an address (owner's request 18.11.2026).
    _wake_rest = ""
    if (
        not mentioned and not is_private and not is_guest
        and message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
        and not getattr(message, "forward_origin", None)
    ):
        _wake_hit, _wake_rest_new = bot.group_wake_request(t)
        if _wake_hit:
            mentioned = True
            _wake_rest = _wake_rest_new
    # Start for measuring response latency next.
    _trace_start = time.monotonic()

    if bot._should_only_record_passively(message, t, is_private=is_private, is_guest=is_guest, mentioned=mentioned):
        # Passive background is the only place where state is needed BEFORE the limit.
        await bot._record_passive_group_context(message, bot.get_state(message.chat.id), t)
        lumen_devtrace.record_passive(message.chat.id, t, is_private=is_private, mentioned=mentioned)
        return

    # Check the limit BEFORE get_state: previously, a message rejected by the limit still
    # created a chat record (and rewrote the index), inflating state and evicting
    # real chats (audit 26.09.2026).
    if not skip_rate_limit and await bot._reject_rate_limited_message(message):
        return

    # The global daily limit — before any processing and before get_state: a refusal does not create
    # neither a chat write, nor a user write, nor messages_received.
    # The TikTok link was removed from this check on 06.11.2026 by owner decision:
    # downloading consumes neither tokens nor a generation, while rejection under the shared limit
    # looked like a broken bot. Once we decide how to pay for
    # download, the condition below is removed together with the comment.
    uid_early = bot._user_key_for_message(message)
    _early_entry = bot._user_daily_peek(uid_early)
    url = extract_url(t)
    wants_tiktok = bool(url) and is_tiktok(url)
    if not wants_tiktok and await _refuse_daily_total(message, uid_early, _early_entry):
        return

    # Reject video/audio before downloading when Gemini is exhausted: a late
    # a check after routing would already have downloaded the file and burned transcription.
    # Skip images — Munito handles them, and the route can survive an exhausted Gemini quota.
    if not wants_tiktok and _direct_attachment_needs_gemini(message) and await _refuse_daily_gemini(message, uid_early, _early_entry):
        return

    state = bot.get_state(message.chat.id)

    # Check for download links (TikTok — always immediately, even in groups without a mention)

    needs_youtube = False
    needs_website = False
    youtube_url_to_analyze: str | None = None
    if url:
        if is_tiktok(url):
            # Count only what reached processing: background and earlier refusals do not count.
            bot._record_stats_event("messages_received")
            await bot.handle_tiktok(message, url)
            return
        # The model reads YouTube/a site itself (file_uri/url_context); the server does not download external content.
        # If it did not open — say so honestly.
        if is_youtube(url):
             needs_youtube = True
             youtube_url_to_analyze = url
        else:
             # needs_website reads the link itself; the model uses url_context on
             # on Google's side — our server does not download arbitrary URLs
             # (its own downloads only through _download_url_bin with a scheme guard).
             needs_website = True

    clean_prompt = bot.clean_mention(_wake_rest or t).strip()

    if clean_prompt and _looks_like_injection_probe(clean_prompt):
        # Injection: reply without an LLM. Do not log the text, only its length (audit M5, 10.2026).
        log.warning('[injection-probe] Blocked a prompt-injection attempt in chat %s (prompt_len=%d)', message.chat.id, len(clean_prompt))
        lumen_devtrace.record_blocked(message.chat.id, reason="injection-probe", prompt_len=len(clean_prompt))
        await bot._safe_reply(message, bot._t(message.chat.id, "injection_probe_reply"))
        return

    lower_prompt = clean_prompt.lower().strip()

    # "continue" after a broken stream: replace the prompt with the original question plus
    # with a mark (the partial answer is already in history). Clarification buttons during continuation do not
    # we show it — the question has already been asked; there is nothing to guess.
    continued_prompt = _continue_after_interrupt(state, clean_prompt)
    is_continuation = continued_prompt is not None
    if is_continuation:
        clean_prompt = continued_prompt
        lower_prompt = clean_prompt.lower().strip()

    # Return to the previous image version: before both repetition and editing, otherwise
    # "return the previous version" would generate a new frame instead of the existing one.
    if not is_continuation:
        _revert_hit, _revert_cut = _match_trigger_with_fillers(
            lower_prompt, REVERT_TRIGGER_PREFIXES,
        )
        _redo_hit, _redo_cut = _match_trigger_with_fillers(
            lower_prompt, REDO_TRIGGER_PREFIXES,
        )
        if (_revert_hit or _redo_hit) and state.get("draw_versions"):
            # “return the previous translation option” — about text, not a frame:
            # letters after the trigger send the request to the ordinary model (10.11.2026 review).
            _rv_trigger = _revert_hit if _revert_hit else _redo_hit
            _rv_body = clean_prompt[(_revert_cut or _redo_cut):]
            _rv_rest = _strip_trigger_content(
                _rv_body, _rv_trigger, message, reply_fallback=False,
            )
            if _is_bare_trigger_request(_rv_rest):
                await bot.revert_draw_version(
                    message, state, redo=bool(_redo_hit and not _revert_hit),
                )
                return

    # "Another version": repeat the last generation with the same description. Only a fresh
    # its own generation — otherwise "again" is ordinary text (a joke, a repeated answer).
    _repeat_hit, _repeat_cut = _match_trigger_with_fillers(
        lower_prompt, REPEAT_TRIGGER_PREFIXES,
    )
    if not is_continuation and _repeat_hit:
        matched_repeat = _repeat_hit
        _last_prompt = state.get("last_draw_prompt")
        _has_draw_ctx = isinstance(_last_prompt, str) and bool(_last_prompt.strip())
        # "another version of the cat in a hat" names a new scene: repeating the old
        # description meant drawing the wrong thing. But drawing from that single word there
        # where no image was requested — a route error: “another translation option
        # letters” and “regenerate the summary” remain text (review 10.11.2026).
        _repeat_rest = _strip_trigger_content(
            clean_prompt[_repeat_cut:], matched_repeat, message, reply_fallback=False,
        )
        # A tail without letters is not a new plot: "another option..." sent the model "..."
        # and drew a mess instead of a cat (live 10.11.2026 16:49).
        _repeat_subject = bool(re.search(r"\w", _repeat_rest, re.UNICODE))
        # Both retries require a fresh OWN generation: without the TTL window, "another option
        # “translate the letter” in a chat where someone drew a week ago burned an image
        # instead of text (review 10.11.2026).
        _repeat_ok = _has_draw_ctx and bot._fresh_generation_ref(state, uid_early)
        if _repeat_subject and _repeat_ok:
            _repeat_count, _repeat_prompt = _split_image_variants(_repeat_rest)
            await bot.inline_draw_variants(
                message, _repeat_prompt or _repeat_rest, variants=_repeat_count,
            )
            return
        if _repeat_ok:
            await bot.inline_draw(message, _last_prompt)
            return

    # A wrapper ("well," "please," "can you") does not change the meaning of the request,
    # but previously broke its recognition (ChatGPT parity, 10.11.2026).
    # A clean start still takes priority: existing routes are unchanged.
    matched_draw_trigger, _draw_cut = _match_trigger_with_fillers(
        lower_prompt, DRAW_TRIGGER_PREFIXES,
    )
    matched_edit_trigger, _edit_cut = _match_trigger_with_fillers(
        lower_prompt, EDIT_TRIGGER_PREFIXES,
    )
    # 'make a picture of a cat' is an order for a new frame, not an edit: the weak verb
    # must not send it down the edit branch (parity 10.11.2026).
    _new_artifact = _is_new_artifact_request(lower_prompt)
    matched_tts_trigger, _tts_cut = _match_trigger_with_fillers(
        lower_prompt, TTS_TRIGGER_PREFIXES,
    )
    # False positives in natural speech: "draw a table of the results" means a table
    # in words, "read the instructions and explain" — analysis, not speech (external
    # audit 16.11.2026). Remove the slice too, otherwise the trigger falls out of the question.
    if matched_draw_trigger and not _draw_trigger_is_visual(
            matched_draw_trigger, lower_prompt[_draw_cut + len(matched_draw_trigger):]):
        matched_draw_trigger, _draw_cut = None, 0
    if matched_tts_trigger and not _tts_trigger_is_speech(
            matched_tts_trigger, lower_prompt[_tts_cut + len(matched_tts_trigger):]):
        matched_tts_trigger, _tts_cut = None, 0
    # Phrase body: the matcher itself supplies the slice — exactly up to the matched trigger.
    _prompt_text = clean_prompt[max(_draw_cut, _edit_cut, _tts_cut):]

    # "Make/draw 2 versions of a cat" is a series of generations, not an edit:
    # check the number BEFORE the edit branch, otherwise a weak verb would consume the request
    # (review 10.11.2026). If there is an attachment/reply, it remains an edit.
    _variant_count = _detect_image_variants(clean_prompt)
    _explicit_media = _msg_media_source(message) is not None or (
        message.reply_to_message is not None and _msg_media_source(message.reply_to_message) is not None
    )
    if (
        _variant_count > 1 and not is_continuation and not _explicit_media
        and (matched_draw_trigger or matched_edit_trigger)
    ):
        _base_prompt = _strip_trigger_content(
            _prompt_text, matched_draw_trigger or matched_edit_trigger, message,
        )
        _count2, _variant_prompt = _split_image_variants(_base_prompt)
        _variant_prompt = _variant_prompt or _base_prompt
        if _variant_prompt:
            await bot.inline_draw_variants(
                message, _variant_prompt, variants=max(_variant_count, _count2),
            )
            return

    # Voice before editing: “make a voice note” starts with a weak verb,
    # and within the fresh-generation window sent the request to image editing — after any
    # audio was unavailable for 10 minutes (review 10.11.2026).
    if matched_tts_trigger:
        tts_content = _strip_trigger_content(_prompt_text, matched_tts_trigger, message)
        if tts_content:
            await bot.inline_tts(message, tts_content)
            return

    # Image editing: a verb + a real reference. Without an image, the text goes
    # an ordinary model (“change the world” is not drawing), so the trigger here is soft.
    if matched_edit_trigger and not is_continuation and not (_new_artifact and not _explicit_media):
        try:
            reference = await bot._resolve_edit_references(
                message, state,
                is_private=is_private,
                implicit=matched_edit_trigger in EDIT_TRIGGER_IMPLICIT,
            )
        except bot._MediaTooLargeError as exc:
            # The file exceeds the download cap: show an honest refusal, otherwise the exception went
            # the global handler, and the person saw no answer (review 10.11.2026).
            await bot._safe_reply(message, bot._t(
                message.chat.id, "media_too_big",
                limit_mb=max(1, round(exc.cap_bytes / (1024 * 1024))),
            ))
            return
        except Exception as exc:
            log.warning("[draw] Edit reference resolve failed, answering as text: %s", exc)
            reference = None
        if reference:
            # The edit prompt is sent in full, including the verb: "change the background to blue"
            # without "change" would become "a blue background" and lose the action.
            # The number of variants belongs here, not in the branch above: there it was cut because of
            # attachment, and “make 3 options: remove the background” silently produced one picture
            # (review 10.11.2026).
            _edit_count, _edit_prompt = _split_image_variants(_prompt_text)
            _edit_count = max(_edit_count, _variant_count)
            if _edit_count > 1:
                await bot.inline_draw_variants(
                    message, _edit_prompt or clean_prompt,
                    variants=_edit_count, reference=reference,
                )
                return
            await bot.inline_draw(message, _prompt_text, reference=reference)
            return

    _ctx_deadline = time.monotonic() + _draw_budget_seconds(bot._msg_remaining_sec())

    if matched_draw_trigger:
        prompt_content = _strip_trigger_content(_prompt_text, matched_draw_trigger, message)
        # "draw this" previously reached the model with the word "this" and drew garbage.
        # Resolve the instruction from chat history; if there is no basis for it, honestly
        # ask for a description (parity 10.11.2026).
        _from_resolver = False
        if not prompt_content or _is_anaphoric_draw_request(prompt_content):
            # With an attached or replied-to photo, "this" refers to it, not to
            # words from history: otherwise a request to draw someone else's picture
            # turned into generation from scratch (parity review 10.11.2026).
            if _explicit_media:
                _anaphora_refs = await bot._resolve_edit_references(
                    message, state, is_private=is_private, implicit=False,
                )
                if _anaphora_refs:
                    await bot.inline_draw(message, clean_prompt, reference=_anaphora_refs)
                    return
            prompt_content = await bot.resolve_draw_request(
                message, _prompt_text, state, _ctx_deadline,
            )
            _from_resolver = bool(prompt_content)
            if not prompt_content:
                await bot._safe_reply(message, bot._t(message.chat.id, "draw_empty"))
                return
        if prompt_content:
            # "Draw X in the style of this photo": transfer the style from the attached photo
            # (as in ChatGPT), rather than ignoring it. Without a photo, use ordinary generation.
            _style_refs = None
            if _wants_style_reference(_prompt_text) and _explicit_media:
                try:
                    _style_refs = await bot._resolve_edit_references(
                        message, state, is_private=is_private, implicit=False,
                    ) or None
                except bot._MediaTooLargeError as exc:
                    await bot._safe_reply(message, bot._t(
                        message.chat.id, "media_too_big",
                        limit_mb=max(1, round(exc.cap_bytes / (1024 * 1024))),
                    ))
                    return
                except Exception as exc:
                    log.warning("[draw] Style reference resolve failed, generating as usual: %s", exc)
            if _from_resolver:
                # The number of options comes from the person's phrase: “3 variants” in the description,
                # returned by the model would turn the series into someone else's text
                # (review 10.11.2026). The description itself is the prompt: repeating "another
                # version" must find the subject, not the word "this," and write it
                # no need to redo it.
                _draw_variants = _split_image_variants(_prompt_text)[0]
                # Remove the quantity phrase from the resolver description too: "3 variants"
                # in someone else's text went to the model and into the prompt (review 17.11.2026).
                _clean_resolved = _strip_variants_phrase(prompt_content)
                _draw_kwargs = {"prepared": _clean_resolved or prompt_content}
                _draw_prompt = _clean_resolved
            else:
                _draw_variants, _draw_prompt = _split_image_variants(prompt_content)
                _draw_kwargs = {}
            if _style_refs:
                _draw_variants = 1
            await bot.inline_draw_variants(
                message, _draw_prompt or prompt_content, variants=_draw_variants,
                reference=_style_refs, **_draw_kwargs,
            )
            return

    # A drawing request without a trigger at the start: "I want a picture with a cat",
    # "need a coffee shop logo", "a cat in a hat, draw it", "picture of a cat"
    # (parity with ChatGPT, 10.11.2026). The question and service verbs are removed by
    # _match_draw_intent, so "how to draw" and "show me" do not reach here.
    if not is_continuation and not _explicit_media:
        _intent, _intent_prompt = _match_draw_intent(lower_prompt, clean_prompt)
        if _intent == "draw" and _intent_prompt:
            _i_count, _i_prompt = _split_image_variants(_intent_prompt)
            await bot.inline_draw_variants(
                message, _i_prompt or _intent_prompt, variants=_i_count,
            )
            return

    # A drawing request without an artifact name: "I want to see a cat on the moon,"
    # "imagine a city on the moon". Resolved by one short contextual question;
    # NONE — and the message goes to the text model, as before.
    if not is_continuation and not _explicit_media and _has_visual_signal(lower_prompt):
        _resolved = await bot.resolve_draw_request(message, clean_prompt, state, _ctx_deadline)
        if _resolved:
            # Here too, the number of options is human-facing: the model could have written
            # “A ginger cat, 3 variants” (review 10.11.2026).
            _r_count, _r_prompt = _split_image_variants(clean_prompt)
            # The quantity phrase is also removed from the description: ", 3 variants"
            # leaked into the prompt via prepared (F-169, 17.11.2026).
            _resolved_clean = _strip_variants_phrase(_resolved) or _resolved
            await bot.inline_draw_variants(
                message, _resolved_clean, variants=_r_count, prepared=_resolved_clean,
            )
            return

    # Clarification buttons for preference requests without details, instead of
    # making the model guess.
    # _pick_resolved is set only by callbacks: without the flag, augmented text would be sent
    # into buttons in a cycle.
    if bot.PICK_BUTTONS_ENABLED and not is_continuation and not getattr(message, "_pick_resolved", False):
        pick_scenario = match_pick_request(lower_prompt)
        if pick_scenario:
            await bot._send_pick_question(message, pick_scenario, clean_prompt)
            return

    # Show “Typing…” immediately, BEFORE downloading the attachment and transcription:
    # otherwise a 20 MB video looked like silence for several seconds
    # (audit 29.10.2026).
    # Do not wait for a response: a round trip to Telegram must not delay the model.
    if not is_guest:
        async def _send_typing() -> None:
            with contextlib.suppress(Exception):
                await bot.bot.send_chat_action(chat_id=message.chat.id, action="typing")

        bot._track_inflight_task(asyncio.create_task(_send_typing()))

    media_too_big_mb = 0
    try:
        med_name, media_tuple = await bot._resolve_incoming_media(
            message, state, clean_prompt, is_private=is_private,
        )
    except bot._MediaTooLargeError as exc:
        # File larger than the download cap: an honest refusal below instead of a blind answer.
        med_name, media_tuple = "", None
        media_too_big_mb = max(1, round(exc.cap_bytes / (1024 * 1024)))
    except Exception as exc:
        # The network/proxy did not return the file: the exception escaped the handler,
        # the user was left without an answer (audit 27.10.2026).
        log.warning("[media] Attachment download failed, replying with a fallback: %s", exc)
        await bot._safe_reply(message, bot._model_error_text("fallback", bot._chat_lang(message.chat.id)))
        return

    if media_too_big_mb and media_tuple is None:
        # The caption must not lead to an answer "as if the file was read": honestly
        # refuse, even if the text is non-empty (audit 28.10.2026).
        await bot._safe_reply(message, bot._t(message.chat.id, "media_too_big", limit_mb=media_too_big_mb))
        return

    # Voice/audio: cheap transcription first; the resulting text then follows the common route by
    # scenario (weight/freshness are determined from what was said). If that fails, it falls back to the previous
    # path: audio goes directly to Gemini (see is_video_or_audio_media in the router).
    # One budget for transcription and routing: otherwise a voice message held the lock beyond its budget.
    route_deadline: float | None = None
    if media_tuple and media_tuple[1].startswith("audio/"):
        route_deadline = bot._deadline_within_message(bot.ROUTE_TOTAL_BUDGET_SEC)
        transcript = await bot._transcribe_audio(media_tuple[0], media_tuple[1], message.chat.id, deadline=route_deadline)
        if transcript:
            clean_prompt = (clean_prompt + "\n" + transcript).strip() if clean_prompt else transcript
            media_tuple = None
            if _looks_like_injection_probe(clean_prompt):
                # The voice transcript is appended after the first prefilter (audit A3-1).
                log.warning('[injection-probe] Blocked a prompt-injection attempt in chat %s (prompt_len=%d)', message.chat.id, len(clean_prompt))
                lumen_devtrace.record_blocked(message.chat.id, reason="injection-probe", prompt_len=len(clean_prompt))
                await bot._safe_reply(message, bot._t(message.chat.id, "injection_probe_reply"))
                return

    # A captionless photo after 'I'll send it now, what suits me?': the model must
    # answer the request rather than receive the service message “describe in detail” (decision 06.11.2026).
    pending = None
    if media_tuple and not clean_prompt and _msg_media_source(message) is not None:
        _pending_sender = message.from_user.id if message.from_user else None
        pending = lumen_memory.pending_instruction(
            state, _pending_sender, now=_message_ts(message),
        )
    if media_tuple and not clean_prompt and pending:
        clean_prompt = (
            pending
            + "\n\n[The user wrote this just before sending the attachment; answer that "
              "request using the attachment, do not describe the attachment on its own.]"
        )
    if media_tuple and not clean_prompt:
         clean_prompt = _ensure_prompt_text(
             None, media_tuple[1], _media_prompt_lang(message, state),
         )
    if youtube_url_to_analyze and not clean_prompt:
         clean_prompt = _youtube_summary_prompt(_media_prompt_lang(message, state))

    if not clean_prompt and not media_tuple and not youtube_url_to_analyze:
         src = _msg_media_source(message)
         if src is not None and type(src).__name__ != "Sticker":
              # The attachment existed, but could not be downloaded or recognized — say so
              #     honestly instead of playing "Listening" into the void.
              if media_too_big_mb:
                   await bot._safe_reply(message, bot._t(message.chat.id, "media_too_big", limit_mb=media_too_big_mb))
              else:
                   await bot._safe_reply(message, bot._model_error_text("fallback", bot._chat_lang(message.chat.id)))
              return
         if mentioned:
              await bot._tg_call(message.reply, bot._t(message.chat.id, "status_listening"))
         return

    media_mime = media_tuple[1] if media_tuple else None
    # A mixed album (photos + video slides): one video is enough for the entire set to be sent
    # to the Gemini branch; otherwise video slides were silently lost (branch review: only media[0] was inspected).
    if media_mime and media_mime.startswith("image/") and extra_media:
        for _, extra_mime in extra_media:
            if not (extra_mime or "").startswith("image/"):
                media_mime = extra_mime
                break
    is_heavy = _looks_like_heavy_query(clean_prompt)
    # An explicit search request also goes to the freshness route; otherwise "Google it" without marker words went to models without search (prod 11.10.2026).
    # "remember, we discussed this yesterday" is a chat-memory question, not a web question: because of the word
    # "yesterday" triggered a search, and the answer was marked "no internet" (measurement 11.11.2026).
    needs_freshness = _wants_web_search(clean_prompt, previous=_previous_user_text(state))
    # Automatic effort selection (Claude-style effort): EFFORT_AUTO=0 preserves the previous
    # a flat MUNITO_REASONING_EFFORT and the previous non-heavy heuristic for Gemini.
    _history_log = state.get("history") or []
    effort: str | None = None
    if _effort_auto_on():
        effort = pick_reasoning_effort(
            clean_prompt, is_heavy=is_heavy, needs_freshness=needs_freshness,
            corrections=recent_corrections(_history_log), history_len=len(_history_log),
        )
    max_searches = agent_max_searches(effort)
    route = _build_route(
        needs_youtube=needs_youtube, needs_website=needs_website,
        media_mime=media_mime, is_heavy=is_heavy, needs_freshness=needs_freshness,
    )
    # Blocks and model counts in them: the full 16-line list was not readable.
    route_shape: list[list[Any]] = []
    for provider, _model_id in route:
        if route_shape and route_shape[-1][0] == provider:
            route_shape[-1][1] += 1
        else:
            route_shape.append([provider, 1])
    # The owner's pin (MUNITO_REASONING_EFFORT) overrides automatic selection at
    # of the request; without it, the log showed the computed level, but a different
    # one was sent
    # (audit 16.11.2026: the log and the fact silently diverged).
    _effort_pin = (os.getenv("MUNITO_REASONING_EFFORT") or "").strip()
    log.info(
        '[router] chat=%s heavy=%s freshness=%s youtube=%s website=%s media=%s effort=%s pin=%s searches<=%d route=%s (%d)',
        message.chat.id, is_heavy, needs_freshness, needs_youtube, needs_website, media_mime,
        effort or "auto-off", _effort_pin or "-", max_searches,
        " -> ".join(f"{p}:{n}" for p, n in route_shape), len(route),
    )

    # Per-user daily limits — it is already too late after downloading/routing, but not before
    # model calls: refusals and failures do not count, only successful answers.
    # A probe without creation: otherwise a refusal would create a record and inflate day_users.
    uid = bot._user_key_for_message(message)
    _late_entry = bot._user_daily_peek(uid)
    if not wants_tiktok and await _refuse_daily_total(message, uid, _late_entry):
        return
    user_gemini_limited = False
    if bot._user_daily_gemini_exhausted(uid, _late_entry):
        # Reserve after the Gemini limit because Munito does not consume Google quota.
        # Gemma, which had its own large quota, also lived here (audit 27.10.2026) but was
        # removed on 05.11.2026. Drop the entire gemini entry and check whether any route
        # remains to show.
        allowed = [(p, m) for p, m in route if p != "gemini"]
        if not allowed:
            # Only Gemini reads links, YouTube, video/audio, and documents.
            await _refuse_daily_gemini(message, uid, _late_entry)
            return
        route = allowed
        user_gemini_limited = True
    # Reserve after the final checks and before any search or model work can await.
    daily_reservation = None
    answer_count_token = None
    if not wants_tiktok:
        from lumen_quotas import _bind_user_daily_reservation, _release_user_daily_reservation

        daily_reservation = bot._reserve_user_daily(uid, total=1)
        if daily_reservation is None:
            if bot._user_daily_admission_blocked(uid):
                bot._record_stats_event("daily_limit_denials")
                await bot._safe_reply(
                    message,
                    bot._model_error_text("fallback", bot._chat_lang(message.chat.id)),
                )
            else:
                await _refuse_daily_total(message, uid, bot._user_daily_peek(uid), force=True)
            return
        if not _bind_user_daily_reservation(uid, daily_reservation):
            _release_user_daily_reservation(uid, daily_reservation)
            await bot._safe_reply(message, bot._model_error_text("fallback", bot._chat_lang(message.chat.id)))
            return
        # Direct or nested core calls do not pass through handle_message's reset.
        answer_count_token = bot._ANSWER_COUNTED.set(False)

    # Processing was reached: background, injections, and refusals above were already filtered out.
    bot._record_stats_event("messages_received")

    # The second zone is substantive content that is not a question: the freshness
    #     regex catches it alone,
    # not only the reserve, when no grounding models remain (the search design calls for searching
    # can do more than Gemini). Only Gemini reads links/YouTube; snippets are no substitute for it.
    # Disabled by a switch or empty — answer with a note.
    need_external_search = (
        needs_freshness
        and not needs_youtube
        and not needs_website
    )
    search_will_run = need_external_search and web_search_is_enabled()
    # Zone 2: the freshness regex was silent, but the message was substantive; the model decides
    # agent loop (prod 15.11.2026: "theoneprobe" answered from memory without searching).
    # Media, links, YouTube, and TikTok use other routes; guests get silence.
    agent_zone2 = (
        not need_external_search
        and web_search_is_enabled()
        and not wants_tiktok
        and not needs_youtube
        and not needs_website
        and media_mime is None
        and not is_guest
        and not search_planner_skipped(clean_prompt)
    )
    # Live status (like Claude): stages in one message, which then becomes
    # answer. Private chats and groups are the owner's decision of 12.11.2026: complaints about a weak
    # to search arrived as screenshots from the group, and without the bubble you cannot see exactly what
    # the loop was built for. Guests stay silent: their answer goes through answerGuestQuery, where
    # edits. The early stage is mandatory: do not slow down ordinary text with a bubble.
    status_failed = False
    try:
        status: StatusMessage | None = None
        status_task: asyncio.Task | None = None
        early_status = search_will_run or bool(media_mime) or needs_website or needs_youtube
        if not is_guest and early_status:
            status_key, status_timed = _initial_status(needs_youtube, needs_website, media_mime, search_will_run)
            status_task = asyncio.create_task(
                StatusMessage.start(message, bot._chat_lang(message.chat.id), status_key, timed=status_timed)
            )

        # Author and reply in the prompt: the model knows who is asking and what is being answered.
        ai_prompt = bot._tagged_prompt(state, message, clean_prompt)
        # The date is needed for fresh questions: without it, the model guesses "tomorrow"
        # (eval 22.10.2026: flash-lite "recalled" a fictional schedule). In tier 2 it
        # need matters more: the model decides 'whether my knowledge is stale' without knowing which
        # today number (review 16.11.2026). Injection before the prompt reaches
        # post-hook retry; it reuses this ai_prompt.
        if needs_freshness or agent_zone2:
            ai_prompt += "\n\n" + _fresh_date_context()
        web_search_failed = False
        snippets_injected = False
        # Close the search stage: one check mark per message, even if the search was
        # attempt 2 after [[SEARCH]] (review 12.11.2026).
        search_step_reported = False
        agent_used = False
        agent_steps = 0
        agent_planning_ms = 0
        if search_will_run or agent_zone2:
            # Snippets go to every provider; otherwise the answer would rely on memory (prod 11.10.2026).
            # Collection finishes before the route budget starts and has its own budget: 22 seconds for one
            # search at medium and 44 s at high/xhigh (evidence_phase_budget_sec), capped
            # remaining MESSAGE_TOTAL_BUDGET_SEC (a parallel question gets "busy").
            # The status bubble is created before the first stage,
            # so loop lines edit the already-live bubble instead of sitting in the queue.
            # Zone 2 has no early bubble (freshness=False): the loop runs silently, and its
            # refusing to search is normal, not “without live search”.
            status = await status_task if status_task is not None else None
            status_task = None
            snippets, model_queries, agent_used, agent_stats = await _gather_web_evidence(
                chat_id=message.chat.id, user_prompt=ai_prompt, clean_prompt=clean_prompt,
                previous=_previous_user_text(state), route=route, effort=effort, status=status,
                # history_meta has not been assembled here yet (it is built below, with media links):
                # past photos, the loop does not fetch them — attach_past is disabled inside.
                deadline=route_deadline, history_meta=None,
                allow_fallback_fetch=search_will_run, zone2=agent_zone2,
            )
            # Loop metrics to the trace and log: otherwise offline work cannot distinguish the loop branch from
            # of the previous search (12.11.2026 review).
            agent_steps = int(agent_stats.get("steps") or 0)
            agent_planning_ms = int(agent_stats.get("planning_ms") or 0)
            agent_pages = list(agent_stats.get("pages") or [])
            if snippets or agent_pages:
                if snippets:
                    ai_prompt += format_snippets_for_prompt(snippets)
                if agent_pages:
                    # Read pages are the same kind of untrusted text, but more complete
                    # snippet: they go in a separate block after the results (audit 16.11.2026).
                    from lumen_read_page import format_pages_for_prompt
                    ai_prompt += format_pages_for_prompt(agent_pages)
                ai_prompt += evidence_trace(model_queries)
                snippets_injected = True
            else:
                # The "no live search" label is honest only for freshness questions: in zone 2
                # the model’s refusal to search is normal, and the marker would be a lie.
                web_search_failed = search_will_run
            # How the sources were obtained — through the loop or the old fetch: by this line in production
            # visible whether the add-on works and whether it silently consumes budget.
            log.info("[web-search] chat=%s sources=%d agent=%s effort=%s",
                     message.chat.id, len(snippets), agent_used, effort or "auto-off")
            if bot._stream_stop_requested(message.chat.id):
                # Stopping during search: there is nothing left to answer, but the stage bubble
                # must end with a marker rather than hang at "Searching..." until timeout.
                log.info("[stop] stopped by the user during the evidence phase")
                # Stopping during search is also an action: otherwise, it is absent from devtrace entirely,
                # and diagnosing "why there was no answer" hits a dead end (audit 16.11.2026).
                lumen_devtrace.record_failure(
                    message.chat.id, user_text=clean_prompt,
                    error="stopped during the evidence phase",
                )
                bot._consume_stream_stop(message.chat.id)
                if status is not None:
                    with contextlib.suppress(Exception):
                        await status.stop()
                    await bot._edit_message_quietly(
                        status.message, bot._t(message.chat.id, "stream_note_stopped").strip())
                bot.mark_state_dirty(message.chat.id)
                return
            if status is not None:
                # One closed stage for the ENTIRE answer: two "searched · found N" entries in the trace
                # read as lies, and "found 1" next to "found nothing" is worse
                # silence (review 12.11.2026). The bubble has no right to crash the response:
                # all its edits are under suppress, as in the loop itself.
                with contextlib.suppress(Exception):
                    if snippets and not search_step_reported:
                        status.add_done("progress_search_done", count=len(snippets))
                        # Read pages are also sources in the bubble: the model can
                        # refer to a URL the person never saw (audit 16.11.2026).
                        _src_items = list(snippets) + [
                            {"url": page.get("url") or "", "publisher": ""} for page in agent_pages]
                        status.set_sources([clean_publisher(item) for item in _src_items if clean_publisher(item)])
                        search_step_reported = True
                    elif not snippets and not search_step_reported:
                        status.add_done("progress_search_empty")
                        search_step_reported = True
                    await status.set_phase("progress_thinking", timed=True)
        elif need_external_search:
            web_search_failed = True
        if status is None and status_task is not None:
            status = await status_task
            status_task = None
        if status is not None and not search_will_run and (media_tuple or extra_media):
            # The file is already downloaded: close the preview stage with a checkmark and show
            # a live thinking timer, like Claude (review 28.10.2026). The edit
            # failure of a bubble must not cost the answer (review 12.11.2026).
            with contextlib.suppress(Exception):
                await status.mark_stage_done()
        # The "no search" note appears only if search really returned nothing: when
        # Gemini's limit is exhausted, snippets go to cheap models instead of being disabled.
        no_search_note = user_gemini_limited and needs_freshness and not snippets_injected
        if no_search_note:
            web_search_failed = False
        if (
            media_tuple is None
            and not extra_media
            and youtube_url_to_analyze is None
            and _media_reference_category(clean_prompt) is not None
        ):
            # Production case 17.09.2026: without a file, the model invented a photo. Append _NO_MEDIA_NOTE; do not trim history.
            ai_prompt += _NO_MEDIA_NOTE
        gemini_media_list = ([media_tuple] if media_tuple else []) + list(extra_media or [])
        gemini_media_list = gemini_media_list or None
        # What to write to the shared log: a caption instead of the service prompt, media links
        # and the "attach previous photos" flag. Bytes do not enter memory, only file_id.
        direct_source = _msg_media_source(message)
        reply_source = _msg_media_source(message.reply_to_message) if message.reply_to_message else None
        media_refs = [
            ref for ref in (
                lumen_memory.media_ref(src)
                for src in ([direct_source] if direct_source is not None else [])
                + list(extra_media_sources or [])
            ) if ref
        ]
        if direct_source is None and reply_source is not None and media_tuple is not None:
            reply_ref = lumen_memory.media_ref(reply_source)
            if reply_ref:
                media_refs.append(reply_ref)
        skip_file_ids = [ref["file_id"] for ref in media_refs]
        # Media was found by the word "photo" in the history: it is already arriving as the
        #     current item, so no duplicate is needed.
        attach_past = not (
            media_tuple is not None and direct_source is None and reply_source is None
        )
        history_text = clean_prompt
        if direct_source is not None and media_tuple is not None:
            # The caption (already without @mention) goes to memory; the internal "describe in detail" and
            # pending request remain only in the current turn prompt.
            history_text = clean_prompt if (message.caption or "").strip() else ""
        _sender = message.from_user
        history_meta = {
            "sender": _flatten_untrusted(bot._note_sender(
                state,
                _sender.id if _sender is not None else None,
                getattr(_sender, "username", None) if _sender is not None else None,
                getattr(_sender, "first_name", None) if _sender is not None else None,
            ), 80),
            "sender_id": _sender.id if _sender is not None else None,
            "ts": _message_ts(message),
            "media": media_refs,
            "addressed": True,
            "skip_file_ids": skip_file_ids,
            "attach_past": attach_past,
        }
        # Streaming (the "live" typing effect) makes sense only for a simple
        # text-only exchange without attachments/YouTube; see _run_route. With the marker
        # "without search", suppress the stream: it is appended to the finished answer.
        # Do not stream a fresh answer without snippets or a grounding head: otherwise it
        # will pass before the post-check, making a retry impossible (production 12.10.2026).
        head_has_live_search = (not route) or route_has_live_search(route[:1])
        allow_stream = (
            not gemini_media_list
            and not youtube_url_to_analyze
            and not no_search_note
            and not is_guest
            and (not needs_freshness or head_has_live_search or snippets_injected)
        )
        if route_deadline is None:
            # One deadline for the first pass and the post-hook retry: previously the retry
            # received a fresh 40 s and exceeded the overall message ceiling.
            route_deadline = bot._deadline_within_message(bot.ROUTE_TOTAL_BUDGET_SEC)
        # The previous-message truncation marker must not affect this.
        state.pop("stream_interrupted", None)
        provenance: dict = {}
        ans = ""
        reply_already_sent = False
        answered_grounded = False
        # The [[SEARCH]] marker from the model: it decided that the fact requires a search,
        # when the wording classifier did not recognize it (measurement 11.11.2026).
        search_requested = False
        stream_bubble: Any = None
        try:
            ans, reply_already_sent = await bot._run_route(
                message.chat.id, ai_prompt, route, message,
                media=gemini_media_list, media_filename=med_name,
                youtube_url=youtube_url_to_analyze, allow_stream=allow_stream,
                deadline=route_deadline, provenance=provenance, status=status,
                history_text=history_text, history_meta=history_meta, effort=effort,
                preserve_grounded_head=needs_freshness,
            )
            answered_grounded = answered_with_live_search(provenance)
        except _SearchRequested as req:
            search_requested = True
            stream_bubble = req.placeholder
        if search_requested and not reply_already_sent:
            # The only additional pass per message: the same status stage,
            # than ordinary search, one fetch from the remaining budget and a retry with the same
            # ai_prompt. Guests cannot receive streams (the answer goes through answerGuestQuery).
            # Creating and recoloring the bubble in this pass is cosmetic: an error
            # Telegram API here would mean the person received nothing at all
            # nothing, although the second pass is still ahead (review 12.11.2026).
            with contextlib.suppress(Exception):
                if status is None and stream_bubble is not None:
                    status = StatusMessage.from_message(stream_bubble, bot._chat_lang(message.chat.id), "progress_searching")
                elif status is None and not is_guest:
                    status = await StatusMessage.start(message, bot._chat_lang(message.chat.id), "progress_searching")
                elif status is not None and not snippets_injected:
                    await status.set_phase("progress_searching")
            mark_snippets: list = []
            mark_queries: list[str] = []
            mark_agent = False
            # The variable is assigned only inside `if not snippets_injected`
            # below, but is always read: on [[SEARCH]] after the already assembled
            # of sources caused an UnboundLocalError, and the message fell into a service
            # an error instead of a response (review 12.11.2026).
            mark_stats: dict = {}
            if not snippets_injected:
                # A definition question is freshness-sensitive: this pass needs a date too.
                # It is already present in fresh and zone-2 prompts (the injection above) — a duplicate
                # in one prompt would read as garbage (review 16.11.2026). The tail, and
                # not the entire prompt: a substring in user text is not our date.
                if "[Context: today is" not in ai_prompt[-80:]:
                    ai_prompt += "\n\n" + _fresh_date_context()
                # The model requested search itself: it writes the queries rather than having them taken
                # the raw phrase — otherwise "[[SEARCH]]" would search for the same words by
                # which the model has already said lacks an answer.
                try:
                    mark_snippets, mark_queries, mark_agent, mark_stats = await _gather_web_evidence(
                        chat_id=message.chat.id, user_prompt=ai_prompt, clean_prompt=clean_prompt,
                        previous=_previous_user_text(state), route=route, effort=effort,
                        status=status, deadline=route_deadline, history_meta=history_meta,
                    )
                except Exception as exc:
                    log.warning("[web-search] Search requested by the model failed: %s", exc.__class__.__name__)
            mark_pages = list(mark_stats.get("pages") or [])
            if mark_snippets or mark_pages:
                if mark_snippets:
                    ai_prompt += format_snippets_for_prompt(mark_snippets)
                if mark_pages:
                    from lumen_read_page import format_pages_for_prompt
                    ai_prompt += format_pages_for_prompt(mark_pages)
                ai_prompt += evidence_trace(mark_queries)
                snippets_injected = True
                # Snippets exist: the "without search" marker would be a lie.
                web_search_failed = False
                no_search_note = False
            else:
                ai_prompt += _SEARCH_UNAVAILABLE_NOTE
                web_search_failed = True
            if status is not None:
                with contextlib.suppress(Exception):
                    if mark_snippets and not search_step_reported:
                        status.add_done("progress_search_done", count=len(mark_snippets))
                        _src_items = list(mark_snippets) + [
                            {"url": page.get("url") or "", "publisher": ""} for page in mark_pages]
                        status.set_sources([clean_publisher(item) for item in _src_items
                                            if clean_publisher(item)])
                        search_step_reported = True
                    elif not mark_snippets and not search_step_reported:
                        # The first pass already set the checkmark: the second line
                        # would contradict the first one.
                        status.add_done("progress_search_empty")
                        search_step_reported = True
                    await status.set_phase("progress_thinking", timed=True)
            log.info("[web-search] The model asked for a search, retrying once (snippets=%d, agent=%s, steps=%s).",
                     len(mark_snippets), mark_agent, mark_stats.get("steps"))
            try:
                # Streaming is needed on the second pass: the person expects live typing, not
                # a dead bubble (allow_stream from the first pass has nothing to do with it).
                ans, reply_already_sent = await bot._run_route(
                    message.chat.id, ai_prompt, route, message,
                    media=gemini_media_list, media_filename=med_name,
                    youtube_url=youtube_url_to_analyze, allow_stream=not is_guest,
                    deadline=route_deadline, provenance=provenance, status=status,
                    history_text=history_text, history_meta=history_meta, effort=effort,
                    preserve_grounded_head=needs_freshness,
                )
                answered_grounded = answered_with_live_search(provenance)
            except _SearchRequested:
                log.warning("[web-search] The model asked for a search again; answering as is.")
                web_search_failed = True
            except Exception as exc:
                log.warning("[web-search] Retry after the model's search request failed: %s", exc.__class__.__name__)
                web_search_failed = True
            if not ans.strip() and not reply_already_sent:
                # The second text pass yielded nothing: no empty message is sent to the chat.
                ans = bot._model_error_text("fallback", bot._chat_lang(message.chat.id))
        if needs_freshness and not search_requested and not snippets_injected and not answered_grounded and not reply_already_sent and not no_search_note:
            # Search before routing found nothing, and the model answered without live
            # search: try again and one retry with snippets; if that fails —
            # an honest marker.
            retry_snippets: list = []
            try:
                retry_budget = web_search_timeout_sec()
                if route_deadline is not None:
                    retry_budget = min(retry_budget, max(0.0, route_deadline - time.monotonic()))
                if (retry_budget > 0 and web_search_is_enabled()
                        and not bot._stream_stop_requested(message.chat.id)):
                    retry_snippets = await fetch_web_snippets(
                        clean_prompt, timeout_sec=retry_budget, max_results=web_search_max_results(),
                        context=_previous_user_text(state),
                    )
            except Exception as exc:
                log.warning("[web-search] Post-hoc snippet fetch failed: %s", exc.__class__.__name__)
            if retry_snippets:
                log.info("[web-search] Retrying route once with fresh snippets (first answer had no live search).")
                # This repeats the earlier search on the raw phrase: model queries
                # already yielded nothing, and the person's wording is another form of the same
                # question, and sometimes it is the one that finds the source. The model also
                # must know, otherwise "I searched for" will refer to the wrong thing.
                ai_prompt += format_snippets_for_prompt(retry_snippets) + evidence_trace([clean_prompt])
                snippets_injected = True
                try:
                    ans, reply_already_sent = await bot._run_route(
                        message.chat.id, ai_prompt, route, message,
                        media=gemini_media_list, media_filename=med_name,
                        youtube_url=youtube_url_to_analyze, allow_stream=False,
                        deadline=route_deadline, provenance=provenance,
                        history_text=history_text, history_meta=history_meta, effort=effort,
                        preserve_grounded_head=needs_freshness,
                    )
                    # The retry already has found snippets: the "without search" marker
                    # would be a lie (audit 28.10.2026).
                    web_search_failed = False
                except Exception as exc:
                    log.warning("[web-search] Retry with snippets failed, keeping first answer with a no-search note: %s", exc.__class__.__name__)
                    web_search_failed = True
            else:
                web_search_failed = True
        elif needs_freshness and not search_requested and not snippets_injected and not answered_grounded:
            log.warning("[web-search] Answer already streamed without live search; cannot retry cleanly.")
        # A fragment in the middle of an answer is not how Claude works: it finishes the thought. If
        # the stream broke after some content was already shown, try once
        # continue from the cutoff point (production 11.11.2026: "generation failed" and
        # the budget ceiling cut the answer mid-phrase).
        _cut_mid_stream = bool(state.get("stream_interrupted"))
        # The provider output ceiling is also a truncated answer: Claude finishes
        # itself, so the bot continues rather than leaving an unexplained fragment.
        _cut_by_cap_now = provenance.get("finish_reason") == "length"
        if _cut_by_cap_now and reply_already_sent:
            state["interrupted"] = True
        _left_sec = (route_deadline - time.monotonic()) if route_deadline is not None else None
        if ((_cut_mid_stream or _cut_by_cap_now) and not is_continuation and reply_already_sent and not is_guest
                and len((ans or "")) > 200 and (_left_sec is None or _left_sec > _CONTINUE_MIN_BUDGET_SEC)):
            continued_prompt = _continue_after_interrupt(state, "продолжи")
            if continued_prompt:
                log.info("[stream] Answer cut mid-stream, continuing once (left=%s).",
                         "none" if _left_sec is None else round(_left_sec, 1))
                try:
                    ans, reply_already_sent = await bot._run_route(
                        message.chat.id, continued_prompt, route, message,
                        allow_stream=True, deadline=route_deadline,
                        provenance=provenance, history_text=continued_prompt,
                        history_meta=history_meta, effort=effort,
                        preserve_grounded_head=needs_freshness,
                    )
                    web_search_failed = False
                except Exception as exc:
                    log.warning("[stream] Auto-continuation failed, keeping the partial answer: %s",
                                exc.__class__.__name__)

        # The 'no search' note is added only if search really returned no result and
        # the model answered without grounding: with live Gemini search, it would have been false.
        note_text = ""
        if no_search_note:
            note_text = bot._t(message.chat.id, "fallback_no_search")
        elif web_search_failed and not answered_grounded:
            note_text = bot._t(message.chat.id, "fallback_no_live_search")
        if note_text:
            ans = note_text + "\n\n" + ans
            if reply_already_sent and status is None:
                # The stream already returned a response, but the bubble is gone: the mark was otherwise lost
                # (review 17.11.2026).
                with contextlib.suppress(Exception):
                    await bot._safe_reply(message, note_text)
            if reply_already_sent and status is not None:
                # The answer is already in the status bubble (streaming or reuse), so
                # write the marker there too, together with the collapsed activity block.
                html_text, plain_text = status.compose_answer(ans)
                if html_text is not None:
                    edited = await bot._tg_call(
                        status.message.edit_text, html_text,
                        parse_mode=ParseMode.HTML, call_timeout=15.0,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    if edited is None:
                        await bot._tg_call(status.message.edit_text, plain_text, parse_mode=None, call_timeout=15.0, link_preview_options=NO_LINK_PREVIEW)
                else:
                    await bot._edit_message_quietly(status.message, ans)
        # The answer hit the provider's output cap: without a truncation marker, the cutoff came
        # as a finished answer (backup corpus 05.11.2026), and "continue" was not offered.
        _cut_by_cap = provenance.get("finish_reason") == "length"
        if _cut_by_cap and not reply_already_sent:
            ans = ans + bot._t(message.chat.id, "stream_note_interrupted")
        if not reply_already_sent:
            await bot._safe_reply(message, ans)
        # A partial stream also counts as "successful" to the caller, but its marker
        # "continue" must survive this block (audit 27.10.2026).
        # A deliberate stop also means "can continue", but auto-continuation does not
        # starts: the stream_interrupted flag is set separately for it.
        _stopped_by_user = bool(state.pop("stopped_by_user", False))
        if state.pop("stream_interrupted", False) or _cut_by_cap or _stopped_by_user:
            state["interrupted"] = True
        else:
            state.pop("interrupted", None)
        bot.mark_state_dirty(message.chat.id)
        # truncated — the provider output cap; partial — an interrupted stream: without this
        # a truncated answer is indistinguishable from a complete one in the trace (backup sample 05.11.2026).
        _truncated = provenance.get("finish_reason") == "length"
        lumen_devtrace.record_answer(
            message.chat.id, user_text=clean_prompt, route=route,
            provider=provenance.get("provider", ""), model=provenance.get("model") or "",
            latency_ms=int((time.monotonic() - _trace_start) * 1000), reply=ans,
            # The background tail comes from the shared log: there is no separate ctx anymore.
            ctx_tail=[
                lumen_memory.format_entry(entry)
                for entry in (state.get("history") or [])
                if isinstance(entry, dict) and entry.get("role") == "user"
                and entry.get("addressed") is False
            ][-3:],
            flags={"fresh": bool(needs_freshness), "snippets": bool(snippets_injected),
                   "grounded": bool(answered_grounded), "streamed": bool(reply_already_sent),
                   "truncated": _truncated, "partial": bool(state.get("interrupted")),
                   "stopped": _stopped_by_user,
                   # Search loop: who brought the sources, how many steps and how much time
                   # went to planning — these fields make the branch visible in the trace.
                   "effort": effort or "auto-off", "agent": bool(agent_used),
                   "agent_steps": agent_steps, "agent_planning_ms": agent_planning_ms},
        )
    except asyncio.CancelledError:
        status_failed = True
        raise
    except Exception as exc:
        status_failed = True
        lumen_devtrace.record_failure(
            message.chat.id, user_text=clean_prompt, route=route,
            error=f"{type(exc).__name__}: {exc}",
            latency_ms=int((time.monotonic() - _trace_start) * 1000),
        )
        if isinstance(exc, (bot.GeminiAllModelsExhaustedError, bot.RouteBudgetExceededError)):
            # Quota/route budget exhaustion is expected, not a bug: warning only, otherwise Sentry drowns (LUMEN-3: 8 noise reports per month).
            log.warning("Chat AI processing hit a known, already-handled outcome: %s", exc)
        else:
            log.exception("Chat AI processing failed:")
        head_model = route[0][1] if route else bot.DEFAULT_GEMINI_MODEL
        if isinstance(exc, bot.GeminiAllModelsExhaustedError):
            await bot._maybe_alert_gemini_exhausted()
        if status is not None:
            # The status bubble must not remain hanging with "Thinking..." after an error.
            # Deleting the bubble is cosmetic: the message is deleted, the network is down, permissions
            # no — none of this may cost the person a service reply,
            # which is written immediately afterward (review 12.11.2026).
            with contextlib.suppress(Exception):
                await status.delete()
            status = None
        await bot._safe_reply(message, bot._route_error_reply_text(exc, head_model, youtube_url_to_analyze=youtube_url_to_analyze, lang=bot._chat_lang(message.chat.id)))
    finally:
        if daily_reservation is not None:
            _release_user_daily_reservation(uid, daily_reservation)
        if answer_count_token is not None:
            bot._ANSWER_COUNTED.reset(answer_count_token)
        if status is None and status_task is not None:
            await discard_status_start(status_task)
        if status is not None:
            # The "typing" indicator is removed in finally: an exception from here
            # would escape the handler after the response had already been sent
            # (review 12.11.2026). An intentional stop leaves the marker and is not deleted.
            with contextlib.suppress(Exception):
                if status_failed:
                    await status.delete()
                else:
                    await status.stop()
                    if getattr(status, "reply_markup", None) is not None:
                        await status.clear_reply_markup()


async def _process_raw_update(raw_update: dict) -> None:
    import bot
    if not isinstance(raw_update, dict):
         return
    try:
        guest = raw_update.get("guest_message")
        if isinstance(guest, dict):
            try:
                 msg_obj = Message.model_validate(guest, context={"bot": bot})
                 guest_user = getattr(msg_obj, "from_user", None)
                 if guest_user is not None and bot._is_banned(guest_user.id):
                     # A banned guest is ignored like a regular message.
                     return
                 gq_id = guest.get("guest_query_id")
                 if gq_id is not None and not getattr(msg_obj, "guest_query_id", None):
                     with contextlib.suppress(Exception):
                         object.__setattr__(msg_obj, "guest_query_id", gq_id)
                 # The same per-chat lock and protections as the regular path
                 # (see _run_under_chat_lock).
                 await _run_under_chat_lock(msg_obj, lambda: bot._handle_message_core(msg_obj))
            except Exception as exc:
                 log.warning("[guest] Guest processing failed: %s", exc)
            return
        upd = Update.model_validate(raw_update, context={"bot": bot})
        await bot.dp.feed_update(bot.bot, upd)
    except Exception as exc:
        log.warning("[update] Raw update processing failed: %s", exc)
