"""Замер персоны по моделям: гоняет вопросы из docs/PROMPT_EVAL.md и ставит баллы по той же шкале.

Зачем: вопрос «какая голова маршрута отвечает ближе к персоне» решается замером,
а не вкусом. Скрипт собирает запросы ровно так, как их собирает бот (системный
промпт из lumen_errors.get_system_prompt, боевой payload со `reasoning_effort`
для Munito), отдаёт ответы LLM-судье по рубрике PROMPT_EVAL.md и печатает
таблицу «модель → сумма баллов».

Ключи провайдеров берутся из переменных окружения или из .env в корне репозитория
(файл уже в .gitignore). Значения ключей нигде не печатаются и не пишутся в отчёт.

    python eval/persona_models.py run --models "munito:deepseek-ai/deepseek-v4.1-flash-1017,gemini:gemini-2.5-flash"
    python eval/persona_models.py report --out <каталог с ответами>

Маршрут бота с 05.11.2026 знает только gemini и munito, поэтому прочие префиксы
провайдеров здесь не принимаются: замер чужой провайдеру смысла не имеет.
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

# Munito: тот же дефолт, что в bot.MUNITO_BASE_URL (значение берём из окружения/.env).
MUNITO_BASE_URL = "https://api.munito.ai/inference/v1"

# Нижняя граница паузы между вызовами: у Gemini 5 RPM, у Munito окна по 30 секунд,
# но быстрее замер не идёт даже когда лимиты модели это позволяют.
CALL_GAP_SEC = float(os.environ.get("EVAL_CALL_GAP_SEC", "2.0"))
JUDGE_MAX_TOKENS = 700

# Лимиты нужны не для красоты: без корректной строки call_gap молотит эндпоинт с
# минимальной паузой, замер ловит 429 и молча теряет модель целиком.
# Gemini — скриншот дашборда AI Studio от 22.10.2026. Munito — живой замер
# eval/munito_probe.py 05.11.2026 по 30-секундным окнам: голова 200 запросов и
# 400K входных токенов, GLM-5.3-Flash 10 запросов и 150K, deepseek-v4-flash того
# же класса, что GLM. Ставим консервативно: пусть замер тормозит, чем ловит 429.
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
# Прикидка токенов на один вызов: системный промпт ~6k плюс история диалога и ответ.
TOKENS_PER_CALL = 7_500
JUDGE_TOKENS_PER_CALL = 1_200
# Судья оценивает чужую персону — свой системный промпт ему не нужен.
JUDGE_SYSTEM_PROMPT = (
    "You are a strict evaluator of chatbot answers. You score answers against a "
    "given rubric and reply with JSON only. You never roleplay as the assistant."
)


# Ответ обязан приходить на языке человека: судья это оценивает «на вкус»,
# а замер 12.11.2026 по всем 25 SUPPORTED_LANGS требует детерминированной
# сверки по графике. Вьетнамский, узбекский, турецкий, филиппинский, зулу и
# европейские паки пишут латиницей — для них латиница и есть свой алфавит,
# а не утечка языка.
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
    """Своя ли графика у ответа: не меньше половины букв в алфавите языка.
    Имя бота в латинице в знаменатель не входит — «Нет, я Lumen.» это русский
    ответ, а не смешение языков (замер 12.11.2026)."""
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
    """Глобальный NO_PROXY владельца содержит «[::1]», и любой httpx-клиент
    (а через него — судебный вызов Gemini) падает на InvalidURL: Invalid port
    (замер 12.11.2026: судья умер на первом же вопросе, ответы были собраны).
    Записные адреса из скобок здесь useless, а прокси-правила остаются.
    Возвращает список переменных, которые пришлось почистить."""
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


def load_env() -> None:
    """Читает .env репозитория в окружение. Значения не печатаются и не логируются."""
    env_file = REPO / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and value and not os.environ.get(name):
            os.environ[name] = value


def load_questions(path: Path | None = None) -> dict:
    # path — альтернативный набор вопросов (например живой диалог для сравнения с Claude).
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


# ── промпт, как в боте ──



def gemini_config(model_id: str, last_user_text: str, *, thinking: bool = True):
    """Конфиг вызова Gemini как в боте: system_instruction, без инструментов поиска.

    thinking=False — для судьи: там размыш полезен, а боевое «не думать на лёгких
    запросах» (thinking_budget=0) мешает оценивать.
    """
    from google.genai import types

    kwargs: dict[str, Any] = {"system_instruction": get_system_prompt(model_id)}
    if thinking and not _looks_like_heavy_query(last_user_text):
        if model_id.startswith("gemini-3"):
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="low")
        elif model_id.startswith("gemini-2.5"):
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    return types.GenerateContentConfig(**kwargs)


# ── вызовы провайдеров ──

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


def _openai_chat(url: str, headers: dict, payload: dict) -> str:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    choices = data.get("choices") or []
    if not choices:
        return ""
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content if isinstance(part, dict)).strip()
    return str(content or "").strip()


def _openai_chat_stream(url: str, headers: dict, payload: dict) -> tuple[str, float, int]:
    """Стриминг chat/completions: (текст, время до первого токена, токенов по usage)."""
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
    """Стриминг Gemini: (текст, время до первого токена, токенов по счётчику API)."""
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
        # Те же поля, что шлёт бот (reasoning_effort из lumen_router_config), чтобы
        # замер мерил боевой маршрут. Судья вызывается со своей системкой и остаётся
        # с размышлением включённым: low-effort судья сама снижает качество оценки.
        from lumen_router_config import _munito_payload_extras
        payload.update(_munito_payload_extras())
    return _openai_chat_stream(url, headers, payload)


def call_model_once(target: Target, history: list[dict], user_text: str, *, system_text: str | None = None) -> tuple[str, float, int]:
    if target.provider == "gemini":
        return call_gemini(target, history, user_text, system_text=system_text)
    return call_munito(target, history, user_text, system_text=system_text)


# Маркер «мне нужен поиск» из системного промпта: офлайн-замер обязан отрабатывать
# его так же, как боевой маршрут, иначе служебная строка из 10 знаков уходит судье
# и вопрос получает ноль (замер 11.11.2026: 43/50, вопросы 22-24).
_SEARCH_MARKER = "[[SEARCH]]"
_NO_SEARCH_NOTE = (
    "\n\n[No web search is available for this turn. Answer from what you already know, "
    "in the user's language, and say in one plain line what you could not verify. "
    "Do not ask for a search and do not refuse to answer. This bracket is an instruction "
    "to you, not text to show: never quote, translate or reproduce it.]"
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
    if text.strip().startswith(_SEARCH_MARKER):
        again, ttft2, tokens2 = _resolve_search_marker(target, history, user_text, system_text)
        return again, ttft + ttft2, tokens + tokens2
    return text, ttft, tokens


def call_with_retry(target: Target, history: list[dict], user_text: str, *, system_text: str | None = None,
                    attempts: int = 5) -> tuple[str, float, int]:
    """Повторяет временные сбои (429/503/high demand): у free-тайера они обычное дело."""
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


# ── замер ──

def call_gap(target: Target, tokens_per_call: int = TOKENS_PER_CALL) -> float:
    """Пауза между вызовами под free-тайер: не выше RPM, не выше TPM.

    tokens_per_call — фактическое значение по счётчику API, а не оценка: промпт
    Lumen тянет ~6k токенов, и без этого окно TPM считается по занижению, замер
    ловит 429 и теряет модель. Нет строки в FREE_TIER_LIMITS — пауза берётся
    минимальная, то есть эндпоинт молотится вслепую.
    """
    limits = FREE_TIER_LIMITS.get(target.label)
    if not limits:
        return CALL_GAP_SEC
    by_rpm = 60.0 / max(1, limits["rpm"])
    by_tpm = (tokens_per_call / max(1, limits["tpm"])) * 60.0 * 1.25
    return max(CALL_GAP_SEC, by_rpm, by_tpm)


def collect_answers(target: Target, questions: list[dict]) -> dict:
    """Прогоняет вопросы по модели, ведёт историю диалога как бот.

    Ошибка в одном вопросе не убивает руку: ответы по остальным вопросам
    набираются, сбой пишется в поле error — иначе одна минутная просадка
    Google обнуляла бы весь замер.
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
                # Судья видит ровно то, что человек: боевой маршрут прогоняет
                # ответ через выходной фильтр, и в память ложится он же.
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
            }
            log(f"  {target.label} вопрос {question['id']}: {len(last)} симв., {tokens_seen} токенов, первый токен {ttft_last:.1f}с, всего {elapsed:.1f}с")
        # Пауза под фактический расход токенов: окно TPM у обеих голов считается по нему,
        # а не по оценке промпта.
        gap = call_gap(target, tokens_seen)
    wrong = sorted((qid for qid, item in answers.items() if item.get("script_ok") is False), key=int)
    if wrong:
        log(f"  язык ответа не совпал с языком вопроса в вопросах: {', '.join(wrong)}")
    return answers


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


def parse_score(raw: str) -> tuple[int, str]:
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
        score = int(data.get("score", 0))
        reason = str(data.get("reason") or "")[:300]
        if score in (0, 1, 2):
            return score, reason
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    for digit in text:
        if digit in "012":
            return int(digit), text[:200]
    return 0, "не удалось разобрать ответ судьи"


def run_judge(judge: Target, question: dict, answers: dict[str, Any], checks: list[int], data: dict) -> dict:
    # Нет ответа — оценивать нечего: это не ноль баллов, а пробел в замере.
    needed = [str(q) for q in checks] if checks else [str(question["id"])]
    for qid in needed:
        item = answers.get(qid) or {}
        if not item.get("answer"):
            return {"error": "нет ответа модели", "score": None}
    prompt = judge_prompt(data, question, answers, checks)
    time.sleep(call_gap(judge, JUDGE_TOKENS_PER_CALL))
    # Судью зовём с нейтральной системкой: с промптом самой Lumen модель
    # подыгрывает персону вместо оценки.
    try:
        raw, _, _ = call_with_retry(judge, [], prompt, system_text=JUDGE_SYSTEM_PROMPT)
    except RuntimeError as exc:
        # Отказ судьи — это не ноль баллов: оценки нет, и она не должна портить итог.
        return {"error": str(exc)[:200], "score": None}
    score, reason = parse_score(raw)
    return {"score": score, "reason": reason}


def projected_calls(target: Target, questions: list[dict]) -> int:
    """Сколько вызовов уйдёт на модель: по одному на каждое сообщение вопроса."""
    return sum(len(q.get("turns") or []) for q in questions if "checks" not in q)


def check_quota_budget(targets: list[Target], questions: list[dict], judge: Target) -> None:
    """Предупреждает, если замер съест дневную квоту (у Gemini 2.5 Flash она 20/сутки)."""
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
    for target in targets:
        log(f"=== ответы: {target.label}")
        try:
            all_answers[target.label] = collect_answers(target, questions)
        except RuntimeError as exc:
            log(f"ОШИБКА {target.label}: {exc}")
            all_answers[target.label] = {"__error__": str(exc)}
    (out_dir / "answers.json").write_text(
        json.dumps(all_answers, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    log(f"=== судья: {judge_target.label}")
    scores: dict[str, dict] = {}
    for target in targets:
        answers = all_answers.get(target.label) or {}
        if "__error__" in answers:
            scores[target.label] = {"error": answers["__error__"], "total": 0, "items": {}}
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
    return 0


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
