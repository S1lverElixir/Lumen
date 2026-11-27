"""Persona measurement across models: runs questions from docs/PROMPT_EVAL.md and scores them on the same scale.

Why: the question “which route head answers closer to the persona” is settled by measurement,
not taste. The script builds requests exactly as the bot does (the system
prompt from lumen_errors.get_system_prompt and the production payload with `reasoning_effort`
for Munito), sends the answers to an LLM judge using the PROMPT_EVAL.md rubric, and prints
a “model → total score” table.

Provider keys come from environment variables and .env in this worktree's root
(the path is derived from the script location). For .env outside the worktree, set LUMEN_ENV_DIR;
the owner's machine path is not assumed by default.
The interval between calls is EVAL_CALL_GAP_SEC (2 s by default). Key values are never
printed or written to the report.

    python eval/persona_models.py run --models "munito:deepseek-ai/deepseek-v4.1-flash-1017,gemini:gemini-2.5-flash"
    python eval/persona_models.py report --out <output directory>

Since 05.11.2026 the bot route has known only gemini and munito, so other provider
prefixes are not accepted here: measuring another provider makes no sense.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
QUESTIONS_FILE = Path(__file__).resolve().parent / "persona_questions.json"
OUT_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Lumen" / "persona-eval"

sys.path.insert(0, str(REPO))

from lumen_errors import get_system_prompt  # noqa: E402
from lumen_router_config import _looks_like_heavy_query  # noqa: E402
from lumen_security import _LATIN_LETTERS_RE, _polish_answer  # noqa: E402

# Munito: the same default as bot.MUNITO_BASE_URL (the value comes from the environment/.env).
MUNITO_BASE_URL = "https://api.munito.ai/inference/v1"

# Lower bound on the gap between calls: Gemini has 5 RPM and Munito has 30-second windows,
# but the measurement does not run faster even when the model limits allow it.
CALL_GAP_SEC = float(os.environ.get("EVAL_CALL_GAP_SEC", "2.0"))
JUDGE_MAX_TOKENS = 700

# Limits are not cosmetic: without a valid call_gap value, the endpoint is hammered with
# the minimum gap, the measurement catches 429 and silently loses the entire model.
# Gemini — AI Studio dashboard screenshot from 22.10.2026. Munito — live measurement
# eval/munito_probe.py on 05.11.2026 over 30-second windows: the head has 200 requests and
# 400K input tokens, GLM-5.3-Flash has 10 requests and 150K, and deepseek-v4-flash is in
# the same class as GLM. Set conservatively: let the measurement slow down rather than catch 429.
FREE_TIER_LIMITS: dict[str, dict[str, int]] = {
    "gemini:gemini-2.5-flash": {"rpm": 5, "tpm": 250_000, "rpd": 20},
    "gemini:gemini-2.5-flash-lite": {"rpm": 10, "tpm": 250_000, "rpd": 20},
    "gemini:gemini-3.8-flash": {"rpm": 5, "tpm": 250_000, "rpd": 20},
    "gemini:gemini-3.7-flash": {"rpm": 5, "tpm": 250_000, "rpd": 20},
    "gemini:gemini-3.6-flash": {"rpm": 5, "tpm": 250_000, "rpd": 20},
    "gemini:gemini-3.5-flash-lite": {"rpm": 15, "tpm": 250_000, "rpd": 500},
    "gemini:gemini-3.1-flash-lite": {"rpm": 15, "tpm": 250_000, "rpd": 500},
    "munito:deepseek-ai/deepseek-v4.1-flash-1017": {"rpm": 60, "tpm": 400_000, "rpd": 250},
    "munito:zai-org/glm-5.3-flash": {"rpm": 15, "tpm": 150_000, "rpd": 100},
    "munito:deepseek-ai/deepseek-v4-flash": {"rpm": 15, "tpm": 150_000, "rpd": 100},
}
# Rough token estimate per call: a ~6k system prompt plus dialog history and the answer.
TOKENS_PER_CALL = 7_500
JUDGE_TOKENS_PER_CALL = 1_200
# The judge evaluates another persona; it does not need its own system prompt.
JUDGE_SYSTEM_PROMPT = (
    "You are a strict evaluator of chatbot answers. You score answers against a "
    "given rubric and reply with JSON only. You never roleplay as the assistant."
)


# The answer must use the person's language: the judge assesses this subjectively,
# while the 12.11.2026 measurement across all 25 SUPPORTED_LANGS requires a deterministic
# script check. Vietnamese, Uzbek, Turkish, Filipino, Zulu, and
# European language packs use Latin — for them, Latin is their own alphabet,
# not a language leak.
_LATIN_SCRIPT_LANGS = frozenset({
    "en", "de", "es", "fr", "it", "nl", "id", "ms", "pt", "fil", "tr", "uz",
    "vi", "pl", "zu",
})
_SCRIPT_BLOCKS = {
    "cyrillic": (0x0400, 0x04FF),
    "arabic": (0x0600, 0x06FF),
    "devanagari": (0x0900, 0x097F),
    "bengali": (0x0980, 0x09FF),
    "thai": (0x0E00, 0x0E7F),
}
_SCRIPT_BY_LANG = {
    "ru": "cyrillic", "uk": "cyrillic", "be": "cyrillic", "kk": "cyrillic",
    "ar": "arabic", "fa": "arabic", "ur": "arabic",
    "hi": "devanagari", "bn": "bengali", "th": "thai",
}


def _script_share(text: str, block: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    lo, hi = _SCRIPT_BLOCKS[block]
    return sum(1 for ch in letters if lo <= ord(ch) <= hi) / len(letters)


def _latin_share(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(len(_LATIN_LETTERS_RE.findall(ch)) for ch in letters) / len(letters)


def answer_script_ok(lang: str | None, answer: str) -> bool:
    """Whether the answer uses the right script: at least half the letters must belong to the language's alphabet.
    The bot name in Latin is excluded from the denominator — “No, I am Lumen.” is a Russian
    answer, not a language mix (measurement 12.11.2026)."""
    if not lang or not answer.strip():
        return False
    text = re.sub(r"(?i)lumen", "", answer)
    if not any(ch.isalpha() for ch in text):
        return True
    if lang in _LATIN_SCRIPT_LANGS:
        return _latin_share(text) >= 0.6
    block = _SCRIPT_BY_LANG.get(lang)
    if block is None:
        return True
    return _script_share(text, block) >= 0.5


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sanitize_no_proxy() -> list[str]:
    """The owner's global NO_PROXY contains “[::1]”, so any httpx client
    (and through it the Gemini judge call) fails with InvalidURL: Invalid port
    (measurement 12.11.2026: the judge died on the very first question after answers were collected).
    Bracketed loopback addresses are useless here; proxy rules remain.
    Returns the list of variables that had to be cleaned."""
    touched: list[str] = []
    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name)
        if not raw:
            continue
        kept = [part for part in raw.split(",") if part.strip() and not part.strip().startswith("[")]
        if kept != [part for part in raw.split(",")]:
            os.environ[name] = ",".join(kept)
            touched.append(name)
    return touched


def env_files() -> list[Path]:
    """This worktree first, then the explicitly configured LUMEN_ENV_DIR; without the variable, only
    this worktree's root, derived from __file__. Values are not printed."""
    here = REPO / ".env"
    configured = os.getenv("LUMEN_ENV_DIR", "").strip()
    if not configured:
        return [here]
    outside = Path(configured).expanduser() / ".env"
    return [here] if here == outside else [here, outside]


def load_env() -> None:
    """Reads the repository .env into the environment. Values are not printed or logged."""
    for env_file in env_files():
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip().lstrip("﻿")
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if name and value and not os.environ.get(name):
                os.environ[name] = value


def load_questions(path: Path | None = None) -> dict:
    # path — an alternative question set (for example, a live dialog for comparison with Claude).
    return json.loads((path or QUESTIONS_FILE).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Target:
    provider: str  # gemini | munito
    model_id: str

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model_id}"

    @property
    def needs_gemini_key(self) -> bool:
        return self.provider == "gemini"


def parse_targets(raw: str) -> list[Target]:
    targets = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        provider, _, model_id = chunk.partition(":")
        provider = provider.strip().lower()
        model_id = model_id.strip()
        if provider not in ("gemini", "munito") or not model_id:
            raise SystemExit(f"не понял модель: {chunk} (ожидается provider:model_id)")
        targets.append(Target(provider, model_id))
    return targets


# ── prompt, as in the bot ──



def gemini_config(model_id: str, last_user_text: str, *, thinking: bool = True):
    """Gemini call configuration as in the bot: system_instruction, with no search tools.

    thinking=False is for the judge: reasoning is useful there, while production “do not think on light
    requests” (thinking_budget=0) hinders evaluation.
    """
    from google.genai import types

    kwargs: dict[str, Any] = {"system_instruction": get_system_prompt(model_id)}
    if thinking and not _looks_like_heavy_query(last_user_text):
        if model_id.startswith("gemini-3"):
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="low")
        elif model_id.startswith("gemini-2.5"):
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    return types.GenerateContentConfig(**kwargs)


# ── provider calls ──

def call_gemini(target: Target, history: list[dict], user_text: str, *, system_text: str | None = None) -> tuple[str, float, int]:
    from google import genai
    from google.genai import types

    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("нет GEMINI_API_KEY (переменная или .env)")
    client = genai.Client(api_key=key)
    contents = [
        types.Content(role=item["role"], parts=[types.Part.from_text(text=item["content"])])
        for item in history
    ]
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_text)]))
    config = gemini_config(target.model_id, user_text, thinking=system_text is None)
    if system_text is not None:
        config = types.GenerateContentConfig(system_instruction=system_text)
    return _gemini_stream(client, target.model_id, contents, config)


def _openai_chat_stream(url: str, headers: dict, payload: dict) -> tuple[str, float, int]:
    """Streaming chat/completions: (text, time to first token, tokens according to usage)."""
    body = {**payload, "stream": True}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    started = time.time()
    ttft = 0.0
    chunks: list[str] = []
    tokens = 0
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            usage = event.get("usage") or {}
            if isinstance(usage.get("total_tokens"), int):
                tokens = max(tokens, usage["total_tokens"])
            for choice in event.get("choices") or []:
                delta = (choice.get("delta") or {}).get("content")
                if isinstance(delta, list):
                    delta = "".join(str(p.get("text") or "") for p in delta if isinstance(p, dict))
                if delta:
                    if not ttft:
                        ttft = time.time() - started
                    chunks.append(delta)
    return "".join(chunks).strip(), ttft, tokens


def _gemini_stream(client: Any, model_id: str, contents: list, config: Any) -> tuple[str, float, int]:
    """Gemini streaming: (text, time to first token, tokens according to the API counter)."""
    started = time.time()
    ttft = 0.0
    chunks: list[str] = []
    tokens = 0
    for chunk in client.models.generate_content_stream(model=model_id, contents=contents, config=config):
        text = getattr(chunk, "text", "") or ""
        if text:
            if not ttft:
                ttft = time.time() - started
            chunks.append(text)
        usage = getattr(chunk, "usage_metadata", None)
        if usage is not None:
            total = getattr(usage, "total_token_count", None)
            if isinstance(total, int) and total > tokens:
                tokens = total
    return "".join(chunks).strip(), ttft, tokens


def call_munito(target: Target, history: list[dict], user_text: str, *, system_text: str | None = None) -> tuple[str, float, int]:
    key = os.environ.get("MUNITO_API_KEY", "")
    if not key:
        raise RuntimeError("нет MUNITO_API_KEY (переменная или .env)")
    base = os.environ.get("MUNITO_BASE_URL", "").strip().rstrip("/") or MUNITO_BASE_URL
    url = f"{base}/chat/completions"
    headers = {"Authorization": f"Bearer {key}"}
    messages = [{"role": "system", "content": system_text or get_system_prompt(target.model_id)}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    payload = {"model": target.model_id, "messages": messages, "stream": False}
    if system_text is None:
        # The same fields the bot sends (reasoning_effort from lumen_router_config), so that
        # the measurement measures the production route. The judge is called with its own system prompt and keeps
        # thinking enabled: a low-effort judge lowers evaluation quality.
        from lumen_router_config import _munito_payload_extras, pick_reasoning_effort
        # Use the same effort-selection rule as the production route: without
        # the argument, _munito_payload_extras returns the old flat low, and the measurement
        # would measure old behavior rather than automatic selection (12.11.2026).
        payload.update(_munito_payload_extras(
            pick_reasoning_effort(user_text), target.model_id,
        ))
    return _openai_chat_stream(url, headers, payload)


def call_model_once(target: Target, history: list[dict], user_text: str, *, system_text: str | None = None) -> tuple[str, float, int]:
    if target.provider == "gemini":
        return call_gemini(target, history, user_text, system_text=system_text)
    return call_munito(target, history, user_text, system_text=system_text)


# The “I need search” marker from the system prompt: the offline measurement must handle
# it exactly like the production route, otherwise the 10-character service line reaches the judge
# and the question gets zero (measurement 11.11.2026: 43/50, questions 22-24).
_SEARCH_MARKER = "[[SEARCH]]"
# The copy must match production (`lumen_message_core._SEARCH_UNAVAILABLE_NOTE`),
# otherwise the persona measurement judges the wrong behavior (review 17.11.2026).
_NO_SEARCH_NOTE = (
    "\n\n[No web search is available for this turn. Answer from what you already know, "
    "in the user's language, and say in one plain line what you could not verify. "
    "Do not ask for a search and do not refuse to answer. "
    "This bracket is an instruction to you, not text to show: never quote, translate or "
    "reproduce it, and never mix its language into the reply.]"
)


def _resolve_search_marker(target: Target, history: list[dict], user_text: str, system_text):
    import asyncio

    from lumen_search import fetch_web_snippets, format_snippets_for_prompt
    try:
        snippets = asyncio.run(fetch_web_snippets(user_text, timeout_sec=8.0)) or []
    except Exception as exc:
        log(f"    поиск в замере не удался: {type(exc).__name__}")
        snippets = []
    block = format_snippets_for_prompt(snippets) if snippets else _NO_SEARCH_NOTE
    log(f"    модель попросила поиск — второй проход со сниппетами ({len(snippets)})")
    return call_model_once(target, history, user_text + block, system_text=system_text)


def call_model(target: Target, history: list[dict], user_text: str, *, system_text: str | None = None) -> tuple[str, float, int]:
    text, ttft, tokens = call_model_once(target, history, user_text, system_text=system_text)
    # The production detector tolerates case, wrappers, and filler; strict startswith
    # shifted scores relative to production (review 17.11.2026).
    from lumen_search import search_marker_requested
    if search_marker_requested(text):
        again, ttft2, tokens2 = _resolve_search_marker(target, history, user_text, system_text)
        return again, ttft + ttft2, tokens + tokens2
    return text, ttft, tokens


def call_with_retry(target: Target, history: list[dict], user_text: str, *, system_text: str | None = None,
                    attempts: int = 5) -> tuple[str, float, int]:
    """Retries transient failures (429/503/high demand): they are routine on a free tier."""
    delays = (8, 20, 40, 60)
    last_error = ""
    for attempt in range(attempts):
        try:
            text, ttft, tokens = call_model(target, history, user_text, system_text=system_text)
            if not text:
                raise RuntimeError("пустой ответ")
            return text, ttft, tokens
        except Exception as exc:
            last_error = str(exc)[:200]
            if attempt == attempts - 1:
                break
            log(f"    {target.label}: сбой ({last_error}) — повтор через {delays[attempt]}с")
            time.sleep(delays[attempt])
    raise RuntimeError(f"{target.label}: {last_error}")


# ── measurement ──

def call_gap(target: Target, tokens_per_call: int = TOKENS_PER_CALL) -> float:
    """Gap between calls under a free tier: no higher than RPM or TPM.

    tokens_per_call is the actual API counter value, not an estimate: the Lumen
    prompt uses ~6k tokens, and without this the TPM window is underestimated, the measurement
    catches 429 and loses the model. With no FREE_TIER_LIMITS entry, the gap is
    minimal, meaning the endpoint is hammered blindly.
    """
    limits = FREE_TIER_LIMITS.get(target.label)
    if not limits:
        return CALL_GAP_SEC
    by_rpm = 60.0 / max(1, limits["rpm"])
    by_tpm = (tokens_per_call / max(1, limits["tpm"])) * 60.0 * 1.25
    return max(CALL_GAP_SEC, by_rpm, by_tpm)


def collect_answers(target: Target, questions: list[dict]) -> dict:
    """Runs questions through the model and maintains dialog history like the bot.

    An error in one question does not kill the run: answers to the remaining questions
    are collected, and the failure is written to the error field — otherwise a one-minute
    Google outage would zero the entire measurement.
    """
    answers: dict[str, Any] = {}
    tokens_seen = TOKENS_PER_CALL
    gap = call_gap(target, tokens_seen)
    log(f"  стартовая пауза {gap:.0f}с (лимиты {FREE_TIER_LIMITS.get(target.label, '—')})")
    for question in questions:
        if "checks" in question:
            continue
        history: list[dict] = []
        last = ""
        ttft_last = 0.0
        started = time.time()
        failure = ""
        for turn in question["turns"]:
            time.sleep(gap)
            try:
                last, ttft_last, tokens_seen = call_with_retry(target, history, turn)
                # The judge sees exactly what the person sees: the production route runs
                # the answer through the output filter, and the same answer goes into memory.
                last = _polish_answer(last, source="persona-eval")
            except RuntimeError as exc:
                failure = str(exc)[:200]
                break
            history.append({"role": "user", "content": turn})
            history.append({"role": "assistant", "content": last})
        elapsed = time.time() - started
        if failure:
            answers[str(question["id"])] = {"turns": question["turns"], "error": failure}
            log(f"  {target.label} вопрос {question['id']}: СБОЙ {failure[:120]}")
        else:
            answers[str(question["id"])] = {
                "turns": question["turns"],
                "answer": last,
                "seconds": round(elapsed, 2),
                "ttft": round(ttft_last, 2),
                "chars": len(last),
                "tokens": tokens_seen,
                "script_ok": answer_script_ok(question.get("lang"), last),
                "style_flags": style_flags(question, last),
            }
            log(f"  {target.label} вопрос {question['id']}: {len(last)} симв., {tokens_seen} токенов, первый токен {ttft_last:.1f}с, всего {elapsed:.1f}с")
        # Gap based on actual token usage: both heads' TPM windows are calculated from it,
        # not from the prompt estimate.
        gap = call_gap(target, tokens_seen)
    wrong = sorted((qid for qid, item in answers.items() if item.get("script_ok") is False), key=int)
    if wrong:
        log(f"  язык ответа не совпал с языком вопроса в вопросах: {', '.join(wrong)}")
    flagged = sorted(((qid, item.get("style_flags")) for qid, item in answers.items() if item.get("style_flags")), key=lambda t: int(t[0]))
    if flagged:
        detail = "; ".join(f"{q}:{','.join(fs)}" for q, fs in flagged)
        log(f"  стиль: флаги в {len(flagged)} вопрос(ах): {detail[:400]}")
    return answers


_EMOJI_CHARS_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF]")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+", re.M)
_HEAD_RE = re.compile(r"^\s*#{1,6}\s+\S.*$", re.M)
_BOLD_LINE_RE = re.compile(r"^\s*\*\*[^*\n]+\*\*\s*$", re.M)


def style_flags(question: dict, answer: str) -> list[str]:
    """Cheap deterministic checks on the answer: they need no judge and
    catch real failures of Claude's style (a service marker in the text, an identity
    leak, an emoji garland, a list where prose was requested, or a dropped
    term). Question hints are in the style field; without them, general norms apply.
    Review 12.11.2026: the judge missed what is counted as a line here."""
    from lumen_security import _leak_spans

    style = question.get("style") or {}
    text = answer or ""
    flags: list[str] = []
    if not text.strip():
        return ["empty"]
    if "[[search]]" in text.lower():
        flags.append("search-marker")
    if _leak_spans(text):
        flags.append("identity-leak")
    n_emoji = len(_EMOJI_CHARS_RE.findall(text))
    max_emoji = style.get("emoji_max", 2)
    if n_emoji > max_emoji:
        flags.append(f"emoji-{n_emoji}>{max_emoji}")
    if style.get("emoji_min") and n_emoji < style["emoji_min"]:
        flags.append(f"emoji-{n_emoji}<asked")
    bullets = len(_BULLET_RE.findall(text))
    if bullets > style.get("bullets_max", 4):
        flags.append(f"bullets-{bullets}")
    heads = len(_HEAD_RE.findall(text)) + len(_BOLD_LINE_RE.findall(text))
    if heads > style.get("heads_max", 2):
        flags.append(f"headings-{heads}")
    marks = text.count("?") + text.count("\u061f") + text.count("\uff1f")
    # Count marks, not questions: a Persian answer to “what to do after
    # being fired” asked ONE question twice (“how much savings do you have? that is,
    # how many months can you last without income?”) — questions-2 was a false positive there
    # (measurement 12.11.2026 on the collected corpus, language fa).
    if "questions_max" in style and marks > style["questions_max"]:
        flags.append(f"questions-{marks}")
    if "questions_min" in style and marks < style["questions_min"]:
        flags.append(f"questions-{marks}<asked")
    if style.get("math") and not any(ch in text for ch in ("$", "\u03c0", "\u2248", "\u221a", "^2", "2\u2070")):
        flags.append("no-math")
    if "chars_max" in style and len(text) > style["chars_max"]:
        flags.append(f"len-{len(text)}>{style['chars_max']}")
    if style.get("table") and "|---" not in text.replace(" ", ""):
        flags.append("no-table")
    for term in style.get("keep_terms") or []:
        if term.lower() not in text.lower():
            flags.append(f"term-lost:{term}")
    return flags


def judge_prompt(data: dict, question: dict, answers: dict[str, Any], checks: list[int]) -> str:
    scale = data["scale"]
    if checks:
        blocks = []
        for qid in checks:
            item = answers.get(str(qid))
            if not item:
                continue
            blocks.append(f"Ответ на вопрос {qid} ({item['turns'][-1]}):\n{item['answer']}")
        body = "\n\n".join(blocks)
    else:
        body = f"Вопрос: {question['turns'][-1]}\n\nОтвет:\n{answers[str(question['id'])]['answer']}"
    return (
        "Ты оцениваешь ответ чат-бота Lumen по строгой шкале персоны. Отвечай ТОЛЬКО JSON.\n\n"
        f"Шкала:\n"
        f"2 — {scale['2']}\n"
        f"1 — {scale['1']}\n"
        f"0 — {scale['0']}\n\n"
        f"Ожидаемое поведение: {question['expect']}\n\n"
        f"{body}\n\n"
        "Верни JSON вида {\"score\": 2, \"reason\": \"одно предложение почему\"}."
    )


def parse_score(raw: str) -> tuple[int | None, str]:
    """Accept an explicit rubric score; ambiguous judge output is a measurement error."""
    text = raw.strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None, "не удалось разобрать ответ судьи"
    if not isinstance(data, dict) or "score" not in data:
        return None, "не удалось разобрать ответ судьи"
    value = data["score"]
    # bool is an int subclass, and int(1.9) silently truncates: neither is a rubric score.
    if type(value) is int and value in (0, 1, 2):
        score = value
    elif isinstance(value, str) and value in ("0", "1", "2"):
        score = int(value)
    else:
        return None, "не удалось разобрать ответ судьи"
    reason = data.get("reason")
    return score, reason[:300] if isinstance(reason, str) else ""


def run_judge(judge: Target, question: dict, answers: dict[str, Any], checks: list[int], data: dict) -> dict:
    # No answer means there is nothing to evaluate: this is a gap in the measurement, not zero points.
    needed = [str(q) for q in checks] if checks else [str(question["id"])]
    for qid in needed:
        item = answers.get(qid) or {}
        if not item.get("answer"):
            return {"error": "нет ответа модели", "score": None}
    prompt = judge_prompt(data, question, answers, checks)
    time.sleep(call_gap(judge, JUDGE_TOKENS_PER_CALL))
    # Call the judge with a neutral system prompt: with Lumen's own prompt, the model
    # roleplays the persona instead of evaluating it.
    try:
        raw, _, _ = call_with_retry(judge, [], prompt, system_text=JUDGE_SYSTEM_PROMPT)
    except RuntimeError as exc:
        # A judge refusal is not zero points: there is no score, and it must not spoil the total.
        return {"error": str(exc)[:200], "score": None}
    score, reason = parse_score(raw)
    if score is None:
        return {"error": reason, "score": None}
    return {"score": score, "reason": reason}


def projected_calls(target: Target, questions: list[dict]) -> int:
    """How many calls go to the model: one for each question message."""
    return sum(len(q.get("turns") or []) for q in questions if "checks" not in q)


def check_quota_budget(targets: list[Target], questions: list[dict], judge: Target) -> None:
    """Warns if the measurement will consume the daily quota (Gemini 2.5 Flash has 20/day)."""
    for target in [*targets, judge]:
        limits = FREE_TIER_LIMITS.get(target.label)
        if not limits:
            continue
        calls = projected_calls(target, questions)
        if target == judge:
            calls = len(questions) * len(targets)
        rpd = limits["rpd"]
        marker = "ВНИМАНИЕ" if calls > rpd else "ок"
        log(f"  {marker}: {target.label} — {calls} вызовов при RPD {rpd}")


def cmd_run(args: argparse.Namespace) -> int:
    load_env()
    data = load_questions(Path(args.questions) if args.questions else None)
    questions = data["questions"]
    if args.only:
        wanted = {int(x) for x in args.only.split(",")}
        questions = [q for q in questions if q["id"] in wanted]
    targets = parse_targets(args.models)
    judge_target = parse_targets(args.judge)[0]
    out_dir = Path(args.out) if args.out else OUT_ROOT / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"каталог: {out_dir}")
    log(f"вопросов: {len(questions)} · моделей: {len(targets)} · судья: {judge_target.label}")
    check_quota_budget(targets, questions, judge_target)

    if args.dry_run:
        log("dry-run: ключи нужны для реальных вызовов")
        for target in targets:
            log(f"  {target.label} — ключ: {'GEMINI_API_KEY' if target.needs_gemini_key else target.provider.upper() + '_API_KEY'}")
        return 0

    all_answers: dict[str, dict] = {}
    had_errors = False
    for target in targets:
        log(f"=== ответы: {target.label}")
        try:
            all_answers[target.label] = collect_answers(target, questions)
        except RuntimeError as exc:
            log(f"ОШИБКА {target.label}: {exc}")
            all_answers[target.label] = {"__error__": str(exc)}
            had_errors = True
    (out_dir / "answers.json").write_text(
        json.dumps(all_answers, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    log(f"=== судья: {judge_target.label}")
    scores: dict[str, dict] = {}
    for target in targets:
        answers = all_answers.get(target.label) or {}
        if "__error__" in answers:
            scores[target.label] = {"error": answers["__error__"], "total": 0, "errors": 1, "items": {}}
            continue
        items: dict[str, Any] = {}
        total = 0
        judged = 0
        errors = 0
        for question in questions:
            checks = question.get("checks") or []
            verdict = run_judge(judge_target, question, answers, checks, data)
            items[str(question["id"])] = verdict
            if verdict.get("score") is None:
                errors += 1
                continue
            total += verdict["score"]
            judged += 1
        had_errors = had_errors or bool(errors)
        scores[target.label] = {
            "total": total,
            "judged": judged,
            "errors": errors,
            "max": 2 * len(questions),
            "items": items,
        }
        log(f"  {target.label}: {total}/{2 * len(questions)} (оценено {judged}, ошибок судьи {errors})")
    (out_dir / "scores.json").write_text(
        json.dumps(scores, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print_report(scores, questions, out_dir, answers=all_answers)
    return 1 if had_errors else 0


def print_report(scores: dict, questions: list[dict], out_dir: Path, answers: dict | None = None) -> None:
    ids = [q["id"] for q in questions]
    labels = list(scores)
    lines = ["| # | " + " | ".join(labels) + " |", "|---|" + "---|" * len(labels)]
    for qid in ids:
        cells = []
        for label in labels:
            item = (scores[label].get("items") or {}).get(str(qid))
            if not item:
                cells.append("-")
            elif item.get("score") is None:
                cells.append("ошибка")
            else:
                cells.append(str(item["score"]))
        lines.append(f"| {qid} | " + " | ".join(cells) + " |")
    lines.append("| **итог** | " + " | ".join(f"{scores[l].get('total', 0)}/{scores[l].get('max', 0)}" for l in labels) + " |")
    text = "\n".join(lines)
    errors = {l: scores[l].get("errors", 0) for l in labels if scores[l].get("errors")}
    if errors:
        text += "\n\nОшибок судьи (оценки нет, в итог не идут): " + " · ".join(f"{k}: {v}" for k, v in errors.items())
    if answers:
        speed = []
        for label in labels:
            items = (answers.get(label) or {}).values()
            ttfts = [i.get("ttft") for i in items if isinstance(i, dict) and i.get("ttft")]
            times = [i.get("seconds") for i in items if isinstance(i, dict) and i.get("seconds")]
            if ttfts and times:
                speed.append(
                    f"{label}: первый токен {sum(ttfts) / len(ttfts):.1f}с, ответ {sum(times) / len(times):.1f}с"
                )
        if speed:
            text += "\n\nСкорость: " + " · ".join(speed)
    print(text)
    (out_dir / "report.md").write_text(text + "\n", encoding="utf-8")


def cmd_report(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    scores = json.loads((out_dir / "scores.json").read_text(encoding="utf-8"))
    questions = load_questions()["questions"]
    print_report(scores, questions, out_dir)
    return 0




def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="прогнать вопросы по моделям и оценить")
    run.add_argument("--models", required=True, help="provider:model через запятую")
    run.add_argument("--judge", default="gemini:gemini-2.5-flash")
    run.add_argument("--only", default=None, help="номера вопросов через запятую")
    run.add_argument("--questions", default=None, help="альтернативный JSON с вопросами, например eval/persona_questions_casual.json")
    run.add_argument("--out", default=None)
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="перепечатать таблицу по сохранённым баллам")
    report.add_argument("--out", required=True)
    report.set_defaults(func=cmd_report)

    sanitize_no_proxy()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
