"""
lumen_security.py — детерминированная защита от промт-инъекций и утечки
идентичности провайдера/модели (Lumen никогда не должен представляться как
Gemini/Gemma/OpenRouter и т.п. — см. system_prompt.py).

Вынесено из bot.py при аудите технического долга: детекторы (_detect_identity_leak,
_detect_injected_payload_echo, _looks_like_injection_probe) — чистые функции над
строками, не зависящие от Telegram/рантайм-состояния бота. Единственная внешняя
зависимость — GEMINI_MODELS/TEXT_MODEL_ORDER из lumen_router_config.py (нужны для
списка точных строк внутренних ID моделей, см. _LEAK_LITERAL_STRINGS ниже).
Публичные имена и поведение не изменились.
"""

from __future__ import annotations

import logging
import re
import unicodedata

from lumen_router_config import GEMINI_MODELS, GEMINI_TTS_MODELS, _KNOWN_MODEL_IDS_FOR_LEAK_DETECTION

# Единый логгер "bot" (а не __name__) — чтобы caplog.at_level(..., logger="bot")
# в тестах продолжал ловить предупреждения независимо от того, в каком
# физическом файле живёт код (см. тот же приём в lumen_router_config.py).
log = logging.getLogger("bot")

# ─────────────────── защита от утечки провайдера/модели (выходной фильтр) ───────────────────
# Системный промпт — слабый рубеж: его уговаривают инъекцией. Поэтому это второй
# рубеж: режем готовый текст ДО отправки и ДО истории, иначе утечка осядет
# в контексте и протечёт в следующие ответы. Если в готовом тексте проскочило
# реальное имя модели/провайдера, весь ответ подменяется на нейтральный fallback.
#
# Слой А — точные строки внутренних ID моделей. Ложных срабатываний практически не
# бывает: обычный ответ на обычный вопрос никогда не должен содержать дефис-разделённый
# технический идентификатор вида "gemini-3.5-flash" или "z-ai/glm-4.5-air:free" — такие
# строки в естественной русской (или английской) речи не встречаются случайно.
_LEAK_LITERAL_STRINGS: tuple[str, ...] = tuple(sorted(
    set(GEMINI_MODELS.keys())
    | set(GEMINI_TTS_MODELS)
    | set(_KNOWN_MODEL_IDS_FOR_LEAK_DETECTION)
))
# Найдено код-ревью: полный перескан стрима это O(n²). Сканим хвост 300 символов:
# самый длинный паттерн 61, стык кусков влезает с запасом.
_LEAK_SCAN_TAIL_CHARS = 300

def _leak_scan_window(full_text: str, latest_piece: str) -> str:
    """Возвращает "хвост" накопленного текста, достаточный для обнаружения ЛЮБОГО
    паттерна утечки, который мог образоваться после добавления latest_piece — без
    необходимости пересканировать весь full_text целиком на каждой итерации стрима.
    Окно берётся с запасом на случай аномально большого одиночного куска."""
    window_size = max(_LEAK_SCAN_TAIL_CHARS, len(latest_piece) + 100)
    return full_text[-window_size:]


# Слой Б — только точные фразы "я — бренд". Широкое окно рядом с "я" ложно ловило
# рассказы про бренды: "я" — частый токен, а хеджирование вроде "я не могу
# сравнивать себя..." — не утечка.
_LEAK_BRAND_TOKENS = (
    r"(gemini|gemma|gpt[\s\-]?oss|chatgpt|openai|claude|anthropic|deepmind|openrouter|"
    r"nemotron|qwen|llama|glm[\s\-]?4|hermes|dolphin[\s\-]?mistral|venice|laguna|"
    r"lfm[\s\-]?2\.5|нейросет\w*\s+google|модел\w*\s+google|google\s*ai|google\s+gemini)"
)
_IDENTITY_LEAK_RE = re.compile(
    rf"\bя\s*(?:—|-|:)?\s*(?:это\s+|являюсь\s+)?{_LEAK_BRAND_TOKENS}\b"
    rf"|\bмен[яе]\s+(?:зовут|называют)\s+{_LEAK_BRAND_TOKENS}\b"
    rf"|\bя\s+созда(?:н|на)\w*\s+(?:компанией\s+)?{_LEAK_BRAND_TOKENS}\b"
    rf"|\bмен[яе]\s+созда(?:л|ла)\w*\s+{_LEAK_BRAND_TOKENS}\b"
    rf"|\bработаю\s+на\s+(?:базе\s+)?{_LEAK_BRAND_TOKENS}\b"
    rf"|\bоснован\w*\s+на\s+{_LEAK_BRAND_TOKENS}\b"
    rf"|\bэт[оауи]\s*(?:модел\w*|нейросет\w*)\s*(?:—|-|:)?\s*{_LEAK_BRAND_TOKENS}\b"
    rf"|\bi\s*(?:am|'m)\s+{_LEAK_BRAND_TOKENS}\b"
    rf"|\bbuilt\s+on\s+{_LEAK_BRAND_TOKENS}\b"
    rf"|\bpowered\s+by\s+{_LEAK_BRAND_TOKENS}\b"
    rf"|\bbased\s+on\s+{_LEAK_BRAND_TOKENS}\b"
    rf"|{_LEAK_BRAND_TOKENS}\s*,?\s*а\s+не\s+lumen\b",
    re.IGNORECASE,
)

_IDENTITY_LEAK_FALLBACK = (
    "Внутренние технические детали своей реализации я не раскрываю. "
    "Если у тебя есть другой вопрос — с радостью помогу."
)

# Слой В — ловим только лексику "подтверждения взлома" из пересказа чужой инструкции.
# Широкий поиск ловил код и честные объяснения джейлбрейков.
_INJECTED_PAYLOAD_ECHO_RE = re.compile(
    r"security[_\s]?breach[_\s]?detected"
    r"|system[_\s]?override[_\s]?(successful|complete)"
    r"|diagnostic[_\s]?success"
    r"|prompt[_\s]?validation[_\s]?successful"
    r"|jailbreak[_\s]?success(ful)?"
    r"|bypass[_\s]?successful"
    r"|injection[_\s]?successful"
    r"|breach[_\s]?detected"
    r"|взлом\s+(прошёл\s+)?успешно"
    r"|инъекция\s+(прошла\s+)?успешно"
    r"|проверка\s+(пройдена|успешна)[:.]?\s*(систем\w*|промпт\w*)",
    re.IGNORECASE,
)

_INJECTED_PAYLOAD_ECHO_FALLBACK = (
    "Это похоже на текст из инструкции, внедрённой в присланный контент, а не на "
    "обычный ответ — воспроизводить его не буду. Если у тебя обычный вопрос, задай "
    "его, и я отвечу."
)

def _detect_injected_payload_echo(text: str) -> bool:
    return bool(text) and bool(_INJECTED_PAYLOAD_ECHO_RE.search(text))

def _detect_identity_leak(text: str) -> bool:
    """Чистая проверка без лога: дёшево вызывать на каждый кусок стрима, лог только в _scrub_identity_leak."""
    if not text:
        return False
    low = text.lower()
    for lit in _LEAK_LITERAL_STRINGS:
        if lit and lit.lower() in low:
            return True
    return bool(_IDENTITY_LEAK_RE.search(text))

# Слой Г — только лог [mush-suspect], без подмены: сигнал смешение письменностей
# внутри токена (прод-кейс nemotron-nano-9b-v2, см. _OR_MODEL_HEALTH).
# Порог 3+ токена от 100 символов эвристический, код/URL исключены.
_SCRIPT_KEYWORDS = (
    "LATIN", "CYRILLIC", "GREEK", "ARMENIAN", "HEBREW", "ARABIC",
    "DEVANAGARI", "BENGALI", "TAMIL", "TELUGU", "KANNADA", "MALAYALAM",
    "GUJARATI", "GURMUKHI", "ORIYA", "SINHALA", "THAI", "LAO", "MYANMAR",
    "KHMER", "GEORGIAN", "THAANA", "HIRAGANA", "KATAKANA", "HANGUL", "CJK",
)
_MUSH_MIN_TEXT_LEN = 100
_MUSH_MIN_MIXED_TOKENS = 3


def _token_scripts(token: str) -> set[str]:
    scripts: set[str] = set()
    for ch in token:
        if not ch.isalpha():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        for keyword in _SCRIPT_KEYWORDS:
            if keyword in name:
                scripts.add(keyword)
                break
    return scripts


def _detect_garbled_mix(text: str) -> bool:
    """True при 3+ смешанных токенах от 100 символов. Короткие не смотрим — там шум выше пользы."""
    if not text or len(text) < _MUSH_MIN_TEXT_LEN:
        return False
    stripped = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    stripped = re.sub(r"`[^`\n]+`", " ", stripped)
    stripped = re.sub(r"https?://\S+", " ", stripped)
    mixed = 0
    for token in re.findall(r"[^\W_]+", stripped, flags=re.UNICODE):
        if len(_token_scripts(token)) >= 2:
            mixed += 1
            if mixed >= _MUSH_MIN_MIXED_TOKENS:
                return True
    return False


def _scrub_identity_leak(text: str, *, source: str) -> str:
    """Точка применения фильтра для НЕстримингового пути (ask_gemini, ask_openrouter_*).
    Вызывается непосредственно перед записью ответа в историю чата — если вызвать её
    только перед показом пользователю, но не перед hist.append/history.append, утечка
    осталась бы в истории и могла бы повлиять на последующие ответы модели."""
    if _detect_identity_leak(text):
        log.warning('[identity-leak] Detected and blocked an identity leak (source=%s): %r', source, text[:500])
        return _IDENTITY_LEAK_FALLBACK
    if _detect_injected_payload_echo(text):
        log.warning('[injection-echo] Detected and blocked a likely injected-instruction echo (source=%s): %r', source, text[:500])
        return _INJECTED_PAYLOAD_ECHO_FALLBACK
    if _detect_garbled_mix(text):
        log.warning('[mush-suspect] Reply looks garbled by multilingual fragments (source=%s): %r', source, text[:500])
    return text

# ─────────────────── защита от промт-инъекций (входной префильтр) ───────────────────
# Входной префильтр: явные взломы режем без LLM, со 100% гарантией. Вопросы "какая
# ты модель" не здесь, их честно разбирает модель.
_INJECTION_PROBE_RE = re.compile(
    r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+instructions"
    r"|забудь\s+(все\s+|про\s+)?(предыдущие\s+|системные\s+)?инструкции"
    r"|игнорируй\s+(все\s+|любые\s+)?(предыдущие\s+|системные\s+)?(инструкции|правила|указания)"
    r"|print\s+your\s+(system\s+)?(prompt|instructions)"
    r"|repeat\s+(everything|the\s+text|all\s+the\s+words)\s+above"
    r"|покажи\s+(мне\s+)?сво(й|и)\s+(системн\w*\s+)?(промпт|инструкции)"
    r"|выведи\s+(мне\s+)?сво(й|и)\s+(системн\w*\s+)?(промпт|инструкции)"
    r"|повтори\s+(всё\s+|весь\s+текст\s+)?(что\s+)?(написано\s+)?выше"
    r"|(developer|debug|god|dan|jailbreak)\s*[\s\-]?mode"
    r"|режим\s+(разработчика|отладки|бога|джейлбрейк\w*)"
    # "в режиме разработчика" только с глаголом (включи/перейди): голое "как включить
    # режим отладки на Android?" легитимно.
    r"|(ты\s+теперь|перейди|перейти|включи|включить|подтверди|подтверждаю)\b[^.?!]{0,60}режиме\s+(разработчика|отладки|бога|джейлбрейк\w*)"
    r"|you\s+are\s+now\s+(an?\s+)?(unrestricted|uncensored|jailbroken)"
    r"|ты\s+теперь\s+(без\s+ограничени\w*|неограничен\w*|не\s+связан\w*\s+правилами)"
    r"|act\s+as\s+(an?\s+)?(unfiltered|uncensored|jailbroken|dan)\b"
    r"|притворись\s*,?\s*(что\s+)?у\s+тебя\s+нет\s+(правил|ограничени\w*)"
    r"|(what|which)\s+(is\s+)?your\s+(real\s+|actual\s+)?system\s+prompt"
    r"|раскрой\s+(свой\s+)?системн\w*\s+промпт",
    re.IGNORECASE,
)

_INJECTION_PROBE_REPLY = (
    "Свою настройку и инструкции я не раскрываю и не обсуждаю в таком формате. "
    "Если у тебя обычный вопрос — задавай, с радостью помогу."
)

def _looks_like_injection_probe(text: str) -> bool:
    """Чистая функция — тестируется отдельно от _handle_message_core."""
    return bool(text) and bool(_INJECTION_PROBE_RE.search(text))
