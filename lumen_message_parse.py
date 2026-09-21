"""
lumen_message_parse.py — разбор входящих сообщений: ссылки, триггерные фразы
/draw и /tts без слэша, указательные маркеры реплая, словесные отсылки к медиа
и одноразовая служебная пометка "файла нет".

Вынесено из bot.py (срез монолита): чистые функции над строками и
конфигурационные данные (списки триггеров, регэкспы) без единой зависимости
от Telegram/Gemini/OpenRouter/рантайм-состояния бота — тот же класс, что
lumen_formatting.py/lumen_security.py. bot.py импортирует нужные имена
напрямую, поэтому `bot.extract_url(...)`, `bot.DRAW_TRIGGER_PREFIXES` и т.д.
продолжают работать ровно как раньше (включая подмену в тестах через
monkeypatch.setattr(bot, ...) — вызовы в bot.py идут через module globals).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

_TIKTOK_RE = re.compile(r"(?:^|\.)tiktok\.com$")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}

def extract_url(text: str) -> str | None:
    m = re.search(r"https?://[^\s]+", text)
    if not m:
         return None
    return m.group(0).rstrip(".,!?;:)>]'\"")

def is_tiktok(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return bool(_TIKTOK_RE.search(host.lower()))
    except Exception:
         return False

def is_youtube(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host in _YOUTUBE_HOSTS
    except Exception:
         return False

# Триггеры только startswith: иначе "как нарисовать..." ложно запустит генерацию.
DRAW_TRIGGER_PREFIXES = [
    "сгенерируй картинку", "сгенерируй мне картинку", "сгенерируй изображение",
    "сгенерируй мне изображение", "создай картинку", "создай мне картинку",
    "создай изображение", "создай мне изображение", "нарисуй картинку", "нарисуй изображение",
    "нарисуй мне", "нарисуй", "нарисуйте", "изобрази картинку", "изобрази", "нарисуй-ка",
    "сгенери картинку", "сгенери изображение", "можешь нарисовать", "можешь нарисовать мне",
]
# "хочу картинку" не триггер: путается с показом/редактурой. Длинные фразы выше коротких, иначе вернётся первое совпадение.
TTS_TRIGGER_PREFIXES = [
    "озвучьте", "озвучь текст", "озвучь мне", "озвуч текст", "озвучь", "озвуч",
    "переведи текст в голос", "переведи слова в голос", "переведи в голос",
    "переведи в аудио", "произнеси текст", "произнеси", "проговори",
    "скажи голосом", "переведи текст в аудио", "переведи слова в аудио",
    "преврати в аудио", "преврати текст в аудио", "преврати это в аудио",
    "конвертируй в аудио", "переведи в звук", "начитай текст", "начитай",
    "зачитай текст", "зачитай", "зачитайте текст", "зачитайте",
    "прочитай вслух", "прочти вслух", "прочтите вслух", "прочтите", "прочти",
    "сделай аудио", "сделай голосовое", "запиши голосовое",
]

def _match_trigger_prefix(text_lower: str, prefixes: list[str]) -> str | None:
    """Первый триггер в начале строки. Нужна граница слова, иначе "прочтите"/"нарисуйка" дадут битую генерацию."""
    for prefix in prefixes:
        if not text_lower.startswith(prefix):
            continue
        rest = text_lower[len(prefix):]
        if rest and rest[0].isalpha():
            continue
        return prefix
    return None

# Пустышки "это/этот текст" значат реплай. Найдено 09.2026: "нарисуй это" рисовало слово "это".
_REPLY_MARKER_RE = re.compile(
    r"^(это|этот|эта|эту|эти|этого|этому|этим|этих)"
    r"(\s+(текст|сообщение|пост|файл|картинк\w*|изображени\w*|видео|фото))?$",
    re.IGNORECASE,
)

def _strip_reply_marker(content: str) -> str:
    """Убирает указательную пустышку из остатка после триггера: если кроме неё
    ничего нет — имелся в виду реплай (возвращает ""). Иначе возвращает остаток
    как есть для буквального использования. Хвостовая пунктуация ("это.",
    "это!") перед проверкой срезается — иначе "озвучь это." чинился бы только
    без точки (найдено код-ревью)."""
    if _REPLY_MARKER_RE.match(content.strip().rstrip(".,!?:;—-").strip()):
        return ""
    return content

# Пометка "файла нет" на один ход модели, в историю не пишем: иначе старые пометки всплывут в следующих ходах.
_NO_MEDIA_NOTE = (
    "\n\n[Служебная пометка — это не слова пользователя: к сообщению не "
    "прикреплён файл, и среди недавних файлов чата подходящего нет. Если "
    "в истории выше нет твоего собственного описания именно этого медиа — "
    "честно скажи, что не видишь файла, и попроси прислать его. "
    "Ничего не выдумывай.]"
)

def _history_user_text(user_text: str) -> str:
    """Текст user-реплики для записи в историю: срезает одноразовую служебную
    пометку (см. _NO_MEDIA_NOTE), если она есть в хвосте. Самому текущему
    запросу к модели пометка уже ушла — здесь остаётся чистый текст."""
    if user_text.endswith(_NO_MEDIA_NOTE):
        return user_text[:-len(_NO_MEDIA_NOTE)]
    return user_text

# Только явные существительные медиа: общие "это/покажи" тянули чужое фото и давали галлюцинации.
# Именованные группы — чтобы понять КАКОЙ тип спросили (регрессия 18.08.2026: "покажи стикер" описывал чужое фото).
_MEDIA_REFERENCE_RE = re.compile(
    r"\b(?:"
    r"(?P<sticker>стикер\w*)|"
    r"(?P<video>видео\w*|видос\w*|ролик\w*|клип\w*|кружок\w*|gif\w*|гиф\w*)|"
    r"(?P<audio>аудио\w*|голосов\w*|войс\w*)|"
    r"(?P<photo>фото\w*|снимок\w*|изображени\w*|скрин\w*|скриншот\w*|картинк\w*)|"
    r"(?P<document>документ\w*|pdf|пдф|файл\w*)"
    r")\b",
    re.IGNORECASE,
)

def _media_reference_category(text: str) -> str | None:
    """Какой ТИП медиа упомянут в тексте ("фото"/"видео"/"аудио"/"стикер"/
    "документ"), если вообще упомянут — None, если явного упоминания нет
    (см. _MEDIA_REFERENCE_RE).
    Используется в _resolve_incoming_media (приоритет №3), чтобы искать в
    recent_media_ids медиа ИМЕННО запрошенного типа, а не слепо последний файл
    независимо от того, что реально спросили."""
    if not text:
        return None
    m = _MEDIA_REFERENCE_RE.search(text)
    if not m:
        return None
    return next(name for name, val in m.groupdict().items() if val is not None)

def _looks_like_media_reference(text: str) -> bool:
    """Требует явного упоминания конкретного типа медиа (фото/видео/аудио/стикер и
    т.п.) — иначе см. регрессию выше. Тестируется отдельно от _handle_message_core."""
    return _media_reference_category(text) is not None

def _mime_matches_media_category(mime: str, category: str) -> bool:
    """Mime подходит категории: стикер только image/webp, фото пережимается в JPEG, этого хватает без объекта Sticker."""
    low = (mime or "").lower()
    if category == "sticker":
        return low == "image/webp"
    if category == "photo":
        return low.startswith("image/") and low != "image/webp"
    if category in ("video", "audio"):
        return low.startswith(f"{category}/")
    if category == "document":
        # PDF/текстовые/офисные файлы + octet-stream (неопознанный бинарник —
        # честная ошибка пользователю всё равно придёт позже из ask_gemini через
        # _is_gemini_supported_mime, а не молчаливое "не нашёл файл").
        return low.startswith(("application/", "text/")) or low == "application/octet-stream"
    return False

# ── Кнопки-уточнения (pick-сценарии) ──
# Pick вместо гадания модели: глагол вкуса + тема + коротко, длинные с деталями идут в модель.
_PICK_VERBS = (
    "посоветуй", "порекомендуй", "подскажи", "накидай", "придумай",
    "recommend", "suggest", "advise",
)
_PICK_TOPICS: dict[str, str] = {
    "фильм": "film", "фильмы": "film", "фильмов": "film",
    "сериал": "series", "сериалы": "series",
    "музык": "music", "песн": "music", "трек": "music",
    "книг": "books", "книж": "books", "книжку": "books",
    "игр": "games", "игру": "games", "игрушку": "games",
    "movie": "film", "movies": "film", "film": "film", "films": "film",
    "series": "series", "show": "series", "shows": "series",
    "music": "music", "song": "music", "songs": "music",
    "track": "music", "tracks": "music",
    "book": "books", "books": "books",
    "game": "games", "games": "games",
}
_PICK_MAX_LEN = 60
PICK_QUESTIONS: dict[str, str] = {
    "film": "Что сегодня хочется?",
    "series": "Что сегодня хочется?",
    "music": "Какое настроение?",
    "books": "Что сегодня хочется?",
    "games": "Во что хочется?",
}
PICK_OPTIONS: dict[str, list[str]] = {
    "film": ["Лёгкое и весёлое", "Драма", "Триллер", "Фантастика"],
    "series": ["Лёгкое и весёлое", "Драма", "Детектив", "Фантастика"],
    "music": ["Энергичное", "Спокойное", "Грустное", "Весёлое"],
    "books": ["Фантастика", "Детектив", "Нон-фикшн", "Классика"],
    "games": ["Экшен", "Стратегия", "RPG", "Головоломка"],
}
# Как выбор дописывается к исходному запросу перед обычным маршрутом.
PICK_CHOICE_TEMPLATES: dict[str, str] = {
    "film": "{original} (жанр: {choice})",
    "series": "{original} (жанр: {choice})",
    "music": "{original} (настроение: {choice})",
    "books": "{original} (жанр: {choice})",
    "games": "{original} (жанр: {choice})",
}

def match_pick_request(text_lower: str) -> str | None:
    """Возвращает id pick-сценария (film/music/...), если текст — вкусовой
    запрос без деталей, иначе None. Проверяется в _handle_message_core до
    маршрута к ИИ: совпавшим запросам показываются кнопки вместо гадания."""
    text = text_lower.strip()
    if not text or len(text) > _PICK_MAX_LEN:
        return None
    # Детали (год, перечисления через запятую) — обычным путём в модель:
    # у такого запроса уже есть вкус, кнопки не нужны.
    if re.search(r"\b(19|20)\d{2}\b", text) or text.count(",") >= 2:
        return None
    if not any(v in text for v in _PICK_VERBS):
        return None
    for topic, scenario in _PICK_TOPICS.items():
        # Латиница строго \b, кириллица префиксом (AUD-E-006): "тигр" не игры.
        if topic.isascii():
            if re.search(r"\b" + re.escape(topic) + r"\b", text):
                return scenario
        elif re.search(r"(?<!\w)" + re.escape(topic), text):
            return scenario
    return None


def _find_recent_media_by_category(bucket: Any, category: str) -> tuple[str, str] | None:
    """Последний элемент бакета ПОДХОДЯЩЕГО типа (не просто последний): "покажи стикер" при [фото] даёт None."""
    if not bucket:
        return None
    for fid, mime in reversed(bucket):
        if _mime_matches_media_category(mime, category):
            return fid, mime
    return None
