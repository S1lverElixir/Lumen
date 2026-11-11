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

_INVISIBLE_RE = re.compile(r"[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u206f]")

def _normalize_leak_text(text: str) -> str:
    """NFKC + удаление невидимых символов: fullwidth и zero-width обходили
    выходной фильтр (аудит 28.10.2026). Гомоглифы (кириллическая «а» в Gemini)
    этим не покрываются — остаточный риск отмечен в отчёте."""
    return _INVISIBLE_RE.sub("", unicodedata.normalize("NFKC", text))


# Слой Б — только точные фразы "я — бренд". Широкое окно рядом с "я" ложно ловило
# рассказы про бренды: "я" — частый токен, а хеджирование вроде "я не могу
# сравнивать себя..." — не утечка.
_LEAK_BRAND_TOKENS = (
    r"(gemini|gemma|gpt[\s\-]?(?:oss|\d)[\w\-]*|chatgpt|openai|claude|anthropic|deepmind|openrouter|"
    r"nemotron|qwen|llama|glm[\s\-]?[45]|hermes|mistral|dolphin[\s\-]?mistral|venice|laguna|"
    r"deepseek|grok|meta(?:\s*ai)?|google|groq|hugging\s*face|huggingface|munito|zhipu|"
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
    # "I am a Gemini model": артикль перед брендом — обычная формулировка,
    # которую старый узкий шаблон пропускал (аудит 28.10.2026). Слова между
    # артиклем и брендом не разрешаем: "I am reading about Gemini" — не утечка.
    rf"|\bi\s*(?:am|'m)\s+(?:an?\s+)?{_LEAK_BRAND_TOKENS}\b"
    # "built on / powered by / based on бренд" тоже только от первого лица,
    # иначе честный рассказ о чужом стеке блокировался целиком.
    rf"|\bi\s*(?:am|'m|was|have\s+been|'ve\s+been)\s+(?:created|made|built|developed|trained|powered)\s+by\s+(?:the\s+|company\s+)?{_LEAK_BRAND_TOKENS}\b"
    # То же с названием роли перед глаголом: "I am an AI model made by Google".
    rf"|\bi\s*(?:am|'m)\s+(?:an?\s+)?(?:ai|language|large\s+language|llm|text|chat)\s+(?:model|assistant|chatbot|ai)\s+(?:created|made|built|developed|trained|powered)\s+by\s+(?:the\s+|company\s+)?{_LEAK_BRAND_TOKENS}\b"
    # "I am a language model from Google" — роль обязательна, иначе ловился бы
    # рассказ "I am reading a book by Google" (аудит 28.10.2026).
    rf"|\bi\s*(?:am|'m)\s+(?:an?\s+)?(?:ai|language|large\s+language|llm|text|chat)\s+(?:model|assistant|chatbot|ai)\s+(?:from|by)\s+(?:the\s+|company\s+)?{_LEAK_BRAND_TOKENS}\b"
    rf"|\bmy\s+creators?\s+(?:is|are)\s+(?:the\s+|company\s+)?{_LEAK_BRAND_TOKENS}\b"
    # "я от Google" только как короткое самоопределение (дальше пунктуация/конец):
    # "я от Google узнал..." — честная фраза, а не утечка.
    rf"|\bя\s+от\s+(?:компании\s+)?{_LEAK_BRAND_TOKENS}(?=\s*[.!?…;:,]|$)"
    rf"|\bмо[йи]\s+создател\w*\s*(?:—|-|:)?\s*(?:компани\w*\s+)?{_LEAK_BRAND_TOKENS}\b"
    rf"|\bя\s*(?:—|-|:)?\s*(?:это\s+|являюсь\s+)?(?:модел\w*|нейросет\w*)\s+от\s+(?:компании\s+)?{_LEAK_BRAND_TOKENS}\b"
    # "Я — языковая модель Google": модель/ассистент + бренд без "от".
    rf"|\bя\s*(?:—|-|:)?\s*(?:это\s+|являюсь\s+)?(?:языков\w*\s+|ии[\s\-]?)?(?:модел\w*|нейросет\w*|ассистент\w*|бот\w*)\s+(?:компании\s+)?{_LEAK_BRAND_TOKENS}\b"
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
    if not text:
        return False
    return bool(_INJECTED_PAYLOAD_ECHO_RE.search(_normalize_leak_text(text)))

def _detect_identity_leak(text: str) -> bool:
    """Чистая проверка без лога: дёшево вызывать на каждый кусок стрима, лог только в _scrub_identity_leak."""
    if not text:
        return False
    text = _normalize_leak_text(text)
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
# Верхний кап сканируемого текста: unicodedata.name() на каждый символ без края —
# CPU-DoS длинным текстом (аудит M10, 10.2026). Хвост за капом не сканируем.
_MUSH_MAX_TEXT_LEN = 20000


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
    text = text[:_MUSH_MAX_TEXT_LEN]
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


# ─────────────────── отделка готового ответа под Claude ───────────────────
# Промпт запрещает и декоративные эмодзи, и смешение языков в ответе, но
# бесплатные головы нарушают оба правила: живой прогон корпуса 11.11.2026 дал
# 12 эмодзи в поздравлении при норме «1-2 на ответ» и «в нём everywhere плавают
# частички» в русском рассказе про небо. Рубеж в коде детерминирован, как скраб
# утечек: показываем то, что показал бы Claude, а не что сгенерировала модель.

# Код и ссылки — не манера речи: эмодзи и латиница внутри них это контент.
# Маска той же длины, что и оригинал, поэтому смещения правок совпадают.
_PROTECTED_SPANS_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`|https?://\S+", re.DOTALL)

# База эмодзи плюс вариативный знак и ZWJ: «🥂🎂» — одна последовательность.
# Служебные значки бота (✻ ✓ ∴ ∵ ⋯ ⋰ ⋱) живут в тех же блоках Dingbats/Math,
# поэтому последовательность проверяется по первому символу — значки бота
# под снятие не попадают.
# Потолок 2 — норма промпта для украшательства; легенда (значок в начале строки
# с разделителем: «🔍 — лупа») — это контент, промпт допускает до пяти таких.
# Свыше потолка украшательства снимаются ВСЕ, а не лишние: частичная правка
# оставляла ответ в двух стилях сразу (ревью 11.11.2026).
_EMOJI_MAX_PER_ANSWER = 2
_EMOJI_MAX_LEGEND = 5
# Значок в ячейке таблицы — это значение столбца, а не декор.
_TABLE_CELL_CH = "|"

_EMOJI_KEEP = "✻✓∴∵⋯⋰⋱"
_EMOJI_BASE = "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF]"
_EMOJI_RUN_RE = re.compile(_EMOJI_BASE + "(?:[\\uFE0F\\u200D]?" + _EMOJI_BASE + "|[\\uFE0F\\u200D])*")
_LEGEND_AFTER_RE = re.compile(r"\s*(?:[—–-]|:|•|\d{1,2}[.)])")
# ZWJ-склейка, вариативный знак и тона кожи: «👨‍👩‍👧» — один знак,
# а не три (ревью 11.11.2026: семья из четырёх кодпоинтов считалась гирляндой).
_EMOJI_JOIN_CH = "\u200d\ufe0f\U0001f3fb\U0001f3fc\U0001f3fd\U0001f3fe\U0001f3ff"


# Только закрытые классы: артикли, предлоги, союзы, указатели, наречия.
# Существительные и полные глаголы — открытые: в техническом русском это
# термины (dict, asyncio, get). Из закрытых убраны все, что в коде работают
# ключевым словом или оператором: not, all, any, each, both (проверка
# 11.11.2026), затем for, in, with, within, without, is, are, was, were, been,
# being, have, has, had, will, would, could, should, shall, must, might, they,
# them, their, there, then, than, over, under, above, below, between, during,
# while, about, against, you, your. Ревью 11.11.2026 посчитало жертвы вида
# «цикл for перебирает» и «экран game over»: снимать такое слово — значить
# испортить правильный ответ, а вставное «the» или «everywhere» остаётся видно.
_FOREIGN_FUNC_WORDS = frozenset({
    "the", "and", "but", "or", "from", "this", "that", "these", "those",
    "very", "quite", "rather", "almost", "enough", "much", "many", "more",
    "most", "other", "another", "such", "some", "every", "few", "less",
    "least", "anything", "something", "everything", "nothing", "because",
    "although", "though", "however", "therefore", "otherwise", "where",
    "wherever", "when", "whenever", "which", "what", "whatever", "who", "whom",
    "whose", "why", "how", "into", "onto", "since", "until", "again",
    "already", "yet", "still", "just", "also", "even", "only", "too", "maybe",
    "perhaps", "really", "actually", "basically", "literally", "clearly",
    "obviously", "finally", "eventually", "always", "never", "often",
    "sometimes", "soon", "now", "here", "everywhere", "somewhere", "anywhere",
    "nowhere", "today", "tonight", "yesterday", "tomorrow",
})

# Сразу за словом стоит один из этих знаков — значит это идентификатор или
# термин (value=, take(2), only-child), а не вставное английское слово.
_CODEISH_AFTER = "([:=/#_.,;+-"
# Предложение-носитель артефакта: не меньше 5 слов не-латинской графики и
# 60% букв в ней. Правило не привязано к кириллице — тот же сбой головы
# одинаково выглядит в тайском, арабском или корейском ответе (проверка
# 25 языков 11.11.2026), а латинское предложение трогать нельзя: там these
# и because — норма, а не артефакт.
_LATIN_LETTERS_RE = re.compile(r"[A-Za-z\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u024F\u1E00-\u1EFF]")
_LETTER_TOKEN_RE = re.compile(r"[^\W\d_]+")
# Целое латинское вхождение вместе с апострофами: «you're» — одно слово,
# а не «you» с хвостом «'re» (ревью 11.11.2026: после снятия оставалось «'re прав»).
_LATIN_RUN_RE = re.compile(r"[A-Za-z][A-Za-z']*", re.ASCII)
_LATIN_RUN_CH = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'"
# Признак того, что в предложении код: английские слова в нём могут быть
# идентификаторами, и вычистивать их нельзя.
_CODE_SIGNAL_RE = re.compile(r"[={}#_]|->|[A-Za-z0-9]+\.[A-Za-z_]+|[A-Z][a-z]+[A-Z]", re.ASCII)
_LANG_MIX_MIN_NONLATIN_WORDS = 5
_LANG_MIX_MIN_NONLATIN_SHARE = 0.6
# Знаки, перед которыми пробел не ставится: вычищая слово, соседний пробел
# убираем вместе с ним, иначе в строке остаётся дыра «улыбки ,».
_TIGHT_PUNCT = ",.;:!?)]»”"


def _masked_copy(text: str) -> str:
    """Текст той же длины, где код и ссылки вытеснены пробелами: по нему ищем
    артефакты, а правим оригинал на тех же смещениях."""
    out = list(text)
    for m in _PROTECTED_SPANS_RE.finditer(text):
        for i in range(m.start(), m.end()):
            out[i] = " "
    return "".join(out)


def _sentence_bounds(text: str) -> list[tuple[int, int]]:
    """Границы предложений без разделителей: правка не переезжает через точку."""
    bounds: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_SPLIT_RE.finditer(text):
        if m.start() > start:
            bounds.append((start, m.start()))
        start = m.end()
    if start < len(text):
        bounds.append((start, len(text)))
    return bounds


def _drop_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Вычёркивает диапазоны и прибирает соседний пробел: перед пунктуацией —
    ведущий, между словами — один из двух, иначе остаётся дыра «улыбки ,» или
    слитное «внёмплавают». Глобальное схлопывание пробелов не делаем — оно
    сжигало бы отступы в коде."""
    if not spans:
        return text
    parts: list[str] = []
    prev = 0
    for s, e in spans:
        s = max(s, prev)
        if s >= e:
            continue
        lead = s > 0 and text[s - 1] == " "
        nxt = text[e] if e < len(text) else ""
        head = s == 0 or text[s - 1] in "\n\r"
        if lead and (nxt in _TIGHT_PUNCT or nxt in "\n\r" or not nxt):
            s -= 1
        elif (lead or head) and nxt == " ":
            e += 1

        parts.append(text[prev:s])
        prev = e
    parts.append(text[prev:])
    return "".join(parts)


def _emoji_clusters(run: str) -> int:
    """Сколько самостоятельных знаков в последовательности: ZWJ склеивает
    соседей в один (семья — это один знак), вариативный знак и тон кожи нового
    кластера не создают. Гирлянда «🎉✨🔥» без ZWJ остаётся тремя."""
    total = 0
    joined = False
    for ch in run:
        if ch == "\u200d":
            joined = True
            continue
        if ch in _EMOJI_JOIN_CH:
            continue
        if not joined:
            total += 1
        joined = False
    return total


def _trim_decoration_emoji(text: str, *, source: str) -> str:
    """Гирлянда decorative-эмодзи урезается до нормы промпта («1-2 на ответ»).
    Эмодзи в начале строки, сразу за которым стоит разделитель («🔍 — лупа»), —
    это легенда, то есть контент: промпт разрешает до пяти таких. В коде,
    ссылках и ячейках таблицы эмодзи — контент, его не считаем и не снимаем."""
    if not text:
        return text
    visible = _masked_copy(text[:_MUSH_MAX_TEXT_LEN])
    legend: list[tuple[tuple[int, int], int]] = []
    flourish: list[tuple[tuple[int, int], int]] = []
    for m in _EMOJI_RUN_RE.finditer(visible):
        if m.group(0)[0] in _EMOJI_KEEP:
            continue
        line_start = visible.rfind("\n", 0, m.start()) + 1
        line_end = visible.find("\n", m.start())
        line_end = len(visible) if line_end < 0 else line_end
        if _TABLE_CELL_CH in visible[line_start:line_end]:
            continue
        at_line_head = not visible[line_start:m.start()].strip()
        size = _emoji_clusters(m.group(0))
        if at_line_head and _LEGEND_AFTER_RE.match(visible, m.end()):
            legend.append((m.span(), size))
        else:
            flourish.append((m.span(), size))
    cut: list[tuple[int, int]] = []
    if sum(size for _, size in flourish) > _EMOJI_MAX_PER_ANSWER:
        cut.extend(span for span, _ in flourish)
    kept = 0
    for span, size in legend:
        if kept + size > _EMOJI_MAX_LEGEND:
            cut.append(span)
        else:
            kept += size
    if not cut:
        return text
    cut.sort()
    log.warning("[emoji-style] Trimmed %d decorative emoji run(s) over the 1-2 norm (source=%s, len=%d)", len(cut), source, len(text))
    return _drop_spans(text, cut)


def _nonlatin_shape(seg: str) -> tuple[int, float]:
    """(слов не-латинской графики, доля не-латинских букв) в отрезке текста."""
    tokens = _LETTER_TOKEN_RE.findall(seg)
    all_letters = sum(len(t) for t in tokens)
    if not all_letters:
        return 0, 0.0
    latin_letters = sum(len(_LATIN_LETTERS_RE.findall(t)) for t in tokens)
    foreign_words = sum(1 for t in tokens if not _LATIN_LETTERS_RE.search(t))
    return foreign_words, (all_letters - latin_letters) / all_letters


def _foreign_insert_spans(text: str) -> list[tuple[int, int]]:
    """Вставные английские служебные слова в предложении, написанном другим
    алфавитом (кириллица, арабица, тайский — любой не-латинский)."""
    if not text or len(text) < _MUSH_MIN_TEXT_LEN:
        return []
    visible = _masked_copy(text[:_MUSH_MAX_TEXT_LEN])
    spans: list[tuple[int, int]] = []
    for lo, hi in _sentence_bounds(visible):
        seg = visible[lo:hi]
        words, share = _nonlatin_shape(seg)
        if words < _LANG_MIX_MIN_NONLATIN_WORDS or share < _LANG_MIX_MIN_NONLATIN_SHARE:
            continue
        if _CODE_SIGNAL_RE.search(seg):
            continue
        for m in _LATIN_RUN_RE.finditer(seg):
            if m.group(0).lower() not in _FOREIGN_FUNC_WORDS:
                continue
            # Остров: слева и справа не латиница. «the beatles» — не вставное
            # «the», а имя: вычищаем только одиночное служебное слово.
            before = seg[m.start() - 1] if m.start() else ""
            after = seg[m.end()] if m.end() < len(seg) else ""
            if before in _LATIN_RUN_CH or after in _LATIN_RUN_CH:
                continue
            if after in _CODEISH_AFTER:
                continue
            spans.append((lo + m.start(), lo + m.end()))
    return spans


def _strip_foreign_inserts(text: str, *, source: str) -> str:
    spans = _foreign_insert_spans(text)
    if not spans:
        return text
    log.warning("[lang-mix] Removed %d inserted Latin word(s) from a non-Latin answer (source=%s, len=%d)", len(spans), source, len(text))
    return _drop_spans(text, spans)


def _polish_answer(text: str, *, source: str) -> str:
    """Единый выходной полировщик ответа: утечки, эмодзи-декор, вставные слова.
    Вызывать там, где стоял _scrub_identity_leak — до показа и до записи в
    историю, иначе артефакт осел бы в контексте следующих ответов.
    Ответ из одних эмодзи — это ответ: если правка съела всё видимое,
    возвращаем оригинал (ревью 11.11.2026: пустой текст уходил в edit_text)."""
    text = _scrub_identity_leak(text, source=source)
    polished = _strip_foreign_inserts(_trim_decoration_emoji(text, source=source), source=source)
    if text.strip() and not polished.strip():
        return text
    return polished


def _polished_prefix_len(text: str, index: int, *, source: str) -> int:
    """Сколько знаков уже показано, если текст укорочен полировкой: длина общего
    префикса оригинала и полированного. Завысить не может, поэтому хвост не
    теряет показанное; при разнобое на самой границе он повторит пару символов,
    что для ответа лучше пропажи (ревью 11.11.2026)."""
    if index <= 0:
        return 0
    polished = _polish_answer(text, source=source)
    shown = _polish_answer(text[:index], source=source)
    limit = min(len(shown), len(polished))
    n = 0
    while n < limit and shown[n] == polished[n]:
        n += 1
    return n


def _leak_spans(text: str) -> list[tuple[int, int]]:
    """Диапазоны символов, где нашлась утечка или эхо инструкции."""
    spans: list[tuple[int, int]] = []
    for pattern in (_LEAK_LITERAL_RE, _IDENTITY_LEAK_RE, _INJECTED_PAYLOAD_ECHO_RE, _IDENTITY_BASED_ON_RE):
        if pattern is None:
            continue
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


# Доля текста с утечкой, выше которой точечная вычистка бессмысленна: резать
# почти весь ответ — хуже, чем честно отказаться (аудит M9, 10.2026).
_SCRUB_MAX_CONTAMINATED_RATIO = 0.5


def _scrub_contaminated_sentences(text: str, *, kind: str) -> str | None:
    """Вырезает только предложения с утечкой. None — текст порезан слишком сильно."""
    spans = _leak_spans(text)
    if not spans:
        return text
    # Границы предложений: режем по .!?… и переводам строк, сохранение разделителя.
    bounds: list[int] = [0]
    for m in _SENTENCE_SPLIT_RE.finditer(text):
        bounds.append(m.end())
    bounds.append(len(text))
    drop: set[int] = set()
    for start, end in spans:
        for idx in range(len(bounds) - 1):
            lo, hi = bounds[idx], bounds[idx + 1]
            if lo <= start < hi or lo < end <= hi:
                drop.add(idx)
    kept = "".join(text[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1) if i not in drop)
    kept = kept.strip()
    if not kept:
        return None
    if len(kept) < len(text) * _SCRUB_MAX_CONTAMINATED_RATIO:
        return None
    log.warning('[%s] Scrubbed only the offending sentence(s), kept %d of %d chars', kind, len(kept), len(text))
    return kept


def _scrub_identity_leak(text: str, *, source: str) -> str:
    """Точка применения фильтра для НЕстримингового пути (ask_gemini, ask_munito_text, ask_munito_multimodal).
    Вызывается непосредственно перед записью ответа в историю чата — если вызвать её
    только перед показом пользователю, но не перед hist.append/history.append, утечка
    осталась бы в истории и могла бы повлиять на последующие ответы модели.

    Раньше любое срабатывание выкидывало ВЕСЬ ответ и подставляло fallback: одна
    вставленная фраза про Gemini посреди нормального объяснения выглядела как поломка
    бота (аудит M9, 10.2026). Теперь режем только предложение с утечкой, а fallback
    оставляем на случай, когда остаток нечитаем."""
    if _detect_identity_leak(text):
        scrubbed = _scrub_contaminated_sentences(text, kind="identity-leak")
        if scrubbed is not None:
            return scrubbed
        log.warning('[identity-leak] Detected and blocked an identity leak (source=%s, len=%d)', source, len(text))
        return _IDENTITY_LEAK_FALLBACK
    if _detect_injected_payload_echo(text):
        scrubbed = _scrub_contaminated_sentences(text, kind="injection-echo")
        if scrubbed is not None:
            return scrubbed
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

# Омоглифы кириллица/латиница: NFKC их не сводит, а модель читает ("Іgnore"
# с украинской і выглядит как ignore). Сворачиваем двойников к латинице перед
# проверкой (прод-проба 09.10.2026: такой зонд прошёл префильтр, удержала модель).
_HOMOGLYPH_FOLD_TABLE = str.maketrans({
    "а": "a", "е": "e", "ё": "e", "і": "i", "ї": "i", "о": "o", "р": "p",
    "с": "c", "х": "x", "у": "y", "ј": "j", "ѕ": "s", "һ": "h", "ԁ": "d",
    "А": "A", "В": "B", "Е": "E", "І": "I", "Ї": "I", "К": "K", "М": "M",
    "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X", "Ү": "Y",
})

def _looks_like_injection_probe(text: str) -> bool:
    """Чистая функция — тестируется отдельно от _handle_message_core."""
    if not text:
        return False
    norm = unicodedata.normalize("NFKC", text)
    # Невидимки бывают и внутри слова, и вместо пробела: проверяем оба варианта.
    stripped = "".join(ch for ch in norm if unicodedata.category(ch) != "Cf")
    if _INJECTION_PROBE_RE.search(stripped):
        return True
    # Тот же текст со свёрнутыми омоглифами: иначе "Іgnore prevіous
    # іnstructions" с кириллическими буквами проходит мимо.
    if _INJECTION_PROBE_RE.search(stripped.translate(_HOMOGLYPH_FOLD_TABLE)):
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
