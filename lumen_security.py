"""
lumen_security.py — защита от промт-инъекций и утечки идентичности
(Lumen не представляется Gemini/Gemma/OpenRouter — см. system_prompt.py).
Детекторы — чистые функции; внешний список ID моделей — из lumen_router_config.
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
    r"(gemini|gemma|gpt[\s\-]?(?:oss|\d)[\w\-]*|chatgpt|openai|claude|anthropic|deepmind|openrouter|"
    r"nemotron|qwen|llama|glm[\s\-]?4|hermes|mistral|dolphin[\s\-]?mistral|venice|laguna|"
    r"deepseek|grok|meta(?:\s*ai)?|google|"
    r"lfm[\s\-]?2\.5|нейросет\w*\s+google|модел\w*\s+google|google\s*ai|google\s+gemini)"
)
_IDENTITY_LEAK_RE = re.compile(
    rf"\bя\s*(?:—|-|:)?\s*(?:это\s+|являюсь\s+)?{_LEAK_BRAND_TOKENS}\b"
    rf"|\bмен[яе]\s+(?:зовут|называют)\s+{_LEAK_BRAND_TOKENS}\b"
    rf"|\bя\s+созда(?:н|на)\w*\s+(?:компанией\s+)?{_LEAK_BRAND_TOKENS}\b"
    rf"|\bмен[яе]\s+созда(?:л|ла)\w*\s+{_LEAK_BRAND_TOKENS}\b"
    rf"|\bработаю\s+на\s+(?:базе\s+)?{_LEAK_BRAND_TOKENS}\b"
    # "основан на бренде" / "based on бренд" без первого лица не ловим: "Его
    # архитектура основана на Google Transformer" и "The model is based on
    # Google research" честные рассказы, не самоопределение (см. ниже проверку
    # по предложениям с _FIRST_PERSON_RE).
    rf"|\bэт[оауи]\s*(?:модел\w*|нейросет\w*)\s*(?:—|-|:)?\s*{_LEAK_BRAND_TOKENS}\b"
    rf"|\bi\s*(?:am|'m)\s+{_LEAK_BRAND_TOKENS}\b"
    # "built on / powered by / based on бренд" тоже только от первого лица,
    # иначе честный рассказ о чужом стеке блокировался целиком.
    rf"|\bi\s+(?:was\s+)?(?:created|made)\s+by\s+(?:the\s+|company\s+)?{_LEAK_BRAND_TOKENS}\b"
    rf"|\bi\s*(?:am|'m)\s+from\s+(?:the\s+|company\s+)?{_LEAK_BRAND_TOKENS}\b"
    rf"|\bmy\s+creators?\s+(?:is|are)\s+(?:the\s+|company\s+)?{_LEAK_BRAND_TOKENS}\b"
    # "я от Google" только как короткое самоопределение (дальше пунктуация/конец):
    # "я от Google узнал..." — честная фраза, а не утечка.
    rf"|\bя\s+от\s+(?:компании\s+)?{_LEAK_BRAND_TOKENS}(?=\s*[.!?…;:,]|$)"
    rf"|\bмо[йи]\s+создател\w*\s*(?:—|-|:)?\s*(?:компани\w*\s+)?{_LEAK_BRAND_TOKENS}\b"
    rf"|\bя\s*(?:—|-|:)?\s*(?:это\s+|являюсь\s+)?(?:модел\w*|нейросет\w*)\s+от\s+(?:компании\s+)?{_LEAK_BRAND_TOKENS}\b"
    rf"|{_LEAK_BRAND_TOKENS}\s*,?\s*а\s+не\s+lumen\b",
    re.IGNORECASE,
)

# "Основан на" и английские built/powered/based только от первого лица и в
# пределах одного предложения: иначе "я" из соседней фразы тянуло бы чужой
# рассказ ("Я помогу. Его архитектура основана на Google Transformer").
_IDENTITY_BASED_ON_RE = re.compile(
    rf"\bоснован\w*\s+на\s+{_LEAK_BRAND_TOKENS}\b"
    rf"|\bbuilt\s+on\s+{_LEAK_BRAND_TOKENS}\b"
    rf"|\bpowered\s+by\s+{_LEAK_BRAND_TOKENS}\b"
    rf"|\bbased\s+on\s+{_LEAK_BRAND_TOKENS}\b",
    re.IGNORECASE,
)
_FIRST_PERSON_RE = re.compile(r"\b(я|меня|мне|мной|мною|i|me|my)\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"[.!?…\n]+")

# Точные ID моделей ловим как отдельные токены, а не подстрокой: слаг внутри
# более длинного слова ("gemini-3.8-flashback") утечкой не считаем.
_LEAK_LITERAL_RE = re.compile(
    r"(?<![\w/:.\-])(?:" + "|".join(
        re.escape(lit) for lit in sorted(_LEAK_LITERAL_STRINGS, key=len, reverse=True) if lit
    ) + r")(?![\w/:.\-])",
    re.IGNORECASE,
) if _LEAK_LITERAL_STRINGS else None

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
    if _LEAK_LITERAL_RE is not None and _LEAK_LITERAL_RE.search(text):
        return True
    if _IDENTITY_LEAK_RE.search(text):
        return True
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if _IDENTITY_BASED_ON_RE.search(sentence) and _FIRST_PERSON_RE.search(sentence):
            return True
    return False

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


def _garbled_mixed_tokens(text: str) -> list[str]:
    """Смешанные токены (2+ письменности внутри одного): сырьё детектора и проверки эха."""
    if not text or len(text) < _MUSH_MIN_TEXT_LEN:
        return []
    stripped = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    stripped = re.sub(r"`[^`\n]+`", " ", stripped)
    stripped = re.sub(r"https?://\S+", " ", stripped)
    mixed: list[str] = []
    for token in re.findall(r"[^\W_]+", stripped, flags=re.UNICODE):
        if len(_token_scripts(token)) >= 2:
            mixed.append(token)
            if len(mixed) >= _MUSH_MIN_MIXED_TOKENS:
                break
    return mixed


def _detect_garbled_mix(text: str) -> bool:
    """True при 3+ смешанных токенах от 100 символов. Короткие не смотрим — там шум выше пользы."""
    return len(_garbled_mixed_tokens(text)) >= _MUSH_MIN_MIXED_TOKENS


def _is_garbled_echo(answer: str, user_text: str | None) -> bool:
    """Эхо за пользователем, а не каша модели: все смешанные токены уже были в запросе."""
    if not user_text:
        return False
    mixed = _garbled_mixed_tokens(answer)
    if not mixed:
        return False
    low_user = user_text.lower()
    return all(token.lower() in low_user for token in mixed)


def _scrub_identity_leak(text: str, *, source: str) -> str:
    """Точка применения фильтра для НЕстримингового пути (ask_gemini, ask_openrouter_*).
    Вызывается непосредственно перед записью ответа в историю чата — если вызвать её
    только перед показом пользователю, но не перед hist.append/history.append, утечка
    осталась бы в истории и могла бы повлиять на последующие ответы модели."""
    if _detect_identity_leak(text):
        log.warning('[identity-leak] Detected and blocked an identity leak (source=%s, len=%d)', source, len(text))
        return _IDENTITY_LEAK_FALLBACK
    if _detect_injected_payload_echo(text):
        log.warning('[injection-echo] Detected and blocked a likely injected-instruction echo (source=%s, len=%d)', source, len(text))
        return _INJECTED_PAYLOAD_ECHO_FALLBACK
    if _detect_garbled_mix(text):
        log.warning('[mush-suspect] Reply looks garbled by multilingual fragments (source=%s, len=%d)', source, len(text))
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
    # Голое "debug mode" / "режим разработчика" без инъекционного контекста не ловим:
    # "Как включить режим разработчика на Android?" и "Что такое debug mode
    # в Python?" легитимны. Режим разработчика/отладки/бога срабатывает только
    # парой с явным взломом (см. _MODE_WORD_RE + _MODE_CONTEXT_RE ниже).
    r"|(ты\s+теперь|перейди|перейти|включи|включить|подтверди|подтверждаю)\b[^.?!]{0,60}режиме\s+(разработчика|отладки|бога|джейлбрейк\w*)"
    r"|you\s+are\s+now\s+(an?\s+)?(unrestricted|uncensored|jailbroken)"
    r"|ты\s+теперь\s+(без\s+ограничени\w*|неограничен\w*|не\s+связан\w*\s+правилами)"
    r"|act\s+as\s+(an?\s+)?(unfiltered|uncensored|jailbroken|dan)\b"
    r"|притворись\s*,?\s*(что\s+)?у\s+тебя\s+нет\s+(правил|ограничени\w*)"
    r"|(what|which)\s+(is\s+)?your\s+(real\s+|actual\s+)?system\s+prompt"
    r"|раскрой\s+(свой\s+)?системн\w*\s+промпт",
    re.IGNORECASE,
)

# Слово режима без контекста легитимно (Android/Python/Minecraft), с явным
# взломом рядом режем. Контекст без общих глаголов "включи/открой": иначе
# "Как включить режим разработчика на Android?" снова ложно срабатывал.
_MODE_WORD_RE = re.compile(
    r"(developer|debug|god|dan|jailbreak)\s*[\s\-]?mode"
    r"|режим\w*\s+(разработчика|разработчике|отладки|отладке|бога|джейлбрейк\w*)",
    re.IGNORECASE,
)
_MODE_CONTEXT_RE = re.compile(
    r"ты\s+теперь|you\s+are\s+now|act\s+as"
    r"|игнорируй|забудь|ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)"
    r"|unrestricted|uncensored|jailbroken|без\s+ограничени|неограничен|не\s+связан\w*\s+правилами"
    r"|притворись|подтверди|подтверждаю"
    r"|system\s+prompt|системн\w*\s+(промпт|инструкц)"
    r"|(покажи|выведи|раскрой|print|повтори|repeat)\b[^.?!]{0,40}(промпт|конфигурац|инструкц|prompt|instructions|above|выше)",
    re.IGNORECASE,
)

def _looks_like_injection_probe(text: str) -> bool:
    """Чистая функция — тестируется отдельно от _handle_message_core."""
    if not text:
        return False
    norm = unicodedata.normalize("NFKC", text)
    # Невидимки бывают и внутри слова, и вместо пробела: проверяем оба варианта.
    stripped = "".join(ch for ch in norm if unicodedata.category(ch) != "Cf")
    if _INJECTION_PROBE_RE.search(stripped):
        return True
    spaced = "".join(" " if unicodedata.category(ch) == "Cf" else ch for ch in norm)
    if _INJECTION_PROBE_RE.search(spaced):
        return True
    # Режим разработчика/отладки/бога только с инъекционным контекстом: голые
    # вопросы про Android/Python/Minecraft идут модели, явные взломы режем здесь.
    probe = stripped if _MODE_WORD_RE.search(stripped) and _MODE_CONTEXT_RE.search(stripped) else ""
    if probe:
        return True
    return bool(_MODE_WORD_RE.search(spaced) and _MODE_CONTEXT_RE.search(spaced))
