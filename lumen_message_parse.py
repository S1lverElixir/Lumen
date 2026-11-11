"""
lumen_message_parse.py — разбор входящих сообщений: ссылки, триггерные фразы
/draw и /tts без слэша, маркеры реплая, отсылки к медиа, пометка "файла нет".
Чистые функции и конфиги, без зависимости от рантайма бота.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from lumen_errors import _is_empty_like

_TIKTOK_RE = re.compile(r"(?:^|\.)tiktok\.com$")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}

def extract_url(text: str) -> str | None:
    """Первая ссылка в тексте. Вторая и далее игнорируются: TikTok/YouTube-пути
    берут одну ссылку (см. match-обработку в message_core)."""
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
    "ты можешь нарисовать", "можешь нарисовать мне", "сможешь нарисовать", "можете нарисовать",
    "не мог бы ты нарисовать", "мог бы ты нарисовать", "can you draw",
    "could you draw", "please draw", "i want you to draw",
    # EN: /start обещает "draw a cat" — чиним (найдено внешним аудитом: был только русский).
    "generate an image", "generate me an image", "create an image", "draw a picture",
    "draw me", "draw an", "draw a", "paint me", "paint", "sketch",
]
# Глаголы правки картинки: срабатывают только когда есть референс — вложение,
# реплай на фото или (для сильных глаголов) свежее фото автора. Без картинки
# текст уходит обычной модели, поэтому "измени мир" не рисует ничего.
EDIT_TRIGGER_PREFIXES = [
    "отредактируй", "переделай", "перерисуй", "измени", "поменяй", "замени",
    "дорисуй", "подправь", "подкорректируй", "убери", "добавь", "сделай",
    "edit", "modify", "change", "replace", "redraw", "retouch",
    "remove", "add", "make the", "make it", "make them", "turn the", "swap the",
]
# Слабые глаголы ("сделай/убери/добавь") осмысленны только при явной отсылке
# к картинке (реплай/вложение): иначе "сделай мне кофе" стало бы правкой.
EDIT_TRIGGER_IMPLICIT = [
    "отредактируй", "переделай", "перерисуй", "измени", "поменяй", "замени",
    "дорисуй", "подправь", "подкорректируй",
    "edit", "modify", "change", "replace", "redraw", "retouch",
]
# Повтор генерации: «ещё вариант» после картинки повторяет последнее описание
# (свежесть проверяет _fresh_generation_ref). Без свежей генерации — обычный текст,
# поэтому «повтори» здесь нет: оно слишком часто значит «повтори ответ».
REPEAT_TRIGGER_PREFIXES = [
    "ещё вариант", "еще вариант", "ещё варианты", "еще варианты", "другой вариант",
    "другие варианты", "новый вариант", "ещё картинку", "еще картинку",
    "ещё одну картинку", "еще одну картинку",
    "другую картинку",
    "another version", "another variant", "regenerate",
]
# Возврат к предыдущей версии: фото уже лежит у Telegram, провайдер не нужен
# и дневной слот не тратится (паритет с Undo/Redo в ChatGPT, 10.11.2026).
REVERT_TRIGGER_PREFIXES = [
    "верни предыдущий вариант", "верни прошлый вариант", "верни предыдущую картинку",
    "верни прошлую картинку", "верни предыдущее изображение", "предыдущий вариант",
    # Разрешение по пустому хвосту («14.2») позволяет вернуть и короткими формами:
    # раньше «верни предыдущий» и «верни картинку» молча уходили текстовой модели.
    "верни предыдущий", "верни прошлый", "верни картинку", "верни фото",
    "верни изображение", "верни как было", "верни то", "previous version",
    "прошлый вариант", "верни как было", "отмени правку", "отмени последнее изменение",
    "верни назад", "previous version", "back to the previous version", "undo",
]
REDO_TRIGGER_PREFIXES = [
    "верни обратно", "верни последнюю версию", "верни как стало", "повтори правку",
    "redo",
]
# «Нарисуй 2 варианта» как в ChatGPT: число вариантов из промпта, 2-4.
# Ищем только рядом со словом варианта/картинки/изображения, а не любое число.
_VARIANTS_RE = re.compile(
    r"(?<!\d)([2-4])\s*(?:вариант\w*|картинк\w*|изображени\w*|верси\w*|versions?|variants?)(?!\w)",
    re.IGNORECASE,
)
_VARIANTS_WORDS: dict[str, int] = {"два": 2, "две": 2, "три": 3, "четыре": 4, "two": 2, "three": 3, "four": 4}


def _split_image_variants(text: str) -> tuple[int, str]:
    """(сколько вариантов, текст без фразы количества): «2 варианта кота» ->
    (2, «кота»), чтобы число не уезжало модели и в повтор («ещё вариант»)."""
    low = (text or "").lower()
    m = _VARIANTS_RE.search(low)
    if m:
        cleaned = (text[:m.start()] + " " + text[m.end():]).strip()
        return int(m.group(1)), re.sub(r"\s{2,}", " ", cleaned).strip(" ,.-")
    for word, num in _VARIANTS_WORDS.items():
        mm = re.search(r"(?<!\w)" + word + r"\s+(?:вариант\w*|картинк\w*|изображени\w*|versions?|variants?)", low)
        if mm:
            cleaned = (text[:mm.start()] + " " + text[mm.end():]).strip()
            return num, re.sub(r"\s{2,}", " ", cleaned).strip(" ,.-")
    return 1, text


def _detect_image_variants(text: str) -> int:
    """Сколько вариантов просили: 1 — обычная генерация, 2-4 — несколько."""
    return _split_image_variants(text)[0]



# ── Паритет с ChatGPT (10.11.2026): он рисует по любому запросу, где виден
# смысл «сделай картинку», а не по первому слову. Человек пишет «ну нарисуй
# кота», «кот в шляпе, нарисуй», «хочу картинку с котом», «нужен логотип
# кофейни», «make me an image of a cat». При этом вопрос «как нарисовать кота»
# и служебные глаголы («покажи картинку», «сохрани», «переведи») картинкой
# становиться не должны.
# Начало-вопрос или служебный глагол: до них просьбой рисовать не считаем.
_DRAW_BLOCK_LEAD_RE = re.compile(
    r"^(?:как|почему|зачем|что|кто|где|когда|сколько|можно|нужно\s+ли|объясни|расскажи|"
    r"покажи|сохрани|переведи|удали|найди|открой|закрой|отправь|пришли|погоди|сравни|"
    r"how|why|what|who|where|when|explain|show|save|translate|delete|find|open|send)\b",
    re.IGNORECASE,
)
# Обвязка перед глаголом: «ну нарисуй», «мне, пожалуйста, нарисуй», «ты можешь...».
# Одно снятое слово-связки: цепочку «ну ещё вариант» режем по границам,
    # потому что «ещё» — одновременно связка и первое слово триггера повтора
    # (ревью 10.11.2026).
_DRAW_FILLER_ONE_RE = re.compile(
    r"^(?:(?:ну|давай|а|и|вот|мне|пожалуйста|ещё|еще|тоже|также|сначала|для\s+начала|"
    r"please|pls|also|just)\b[,\s]*)",
    re.IGNORECASE,
)


def _filler_offsets(text_lower: str) -> list[int]:
    """Возможные начала просьбы: 0 и позиция после каждого снятого слова."""
    offsets = [0]
    pos = 0
    for _ in range(4):
        m = _DRAW_FILLER_ONE_RE.match(text_lower[pos:])
        if not m:
            break
        pos += m.end()
        offsets.append(pos)
    return offsets


_DRAW_FILLER_RE = re.compile(
    r"^(?:(?:ну|давай|а|и|вот|мне|пожалуйста|ещё|еще|тоже|также|сначала|для\s+начала|"
    r"please|pls|also|just)\b[,\s]*){0,4}",
    re.IGNORECASE,
)
# Артефакт, который просят создать (в ChatGPT это и шаблоны: постер, логотип, обложка).
_IMAGE_ARTIFACT_RE = re.compile(
    r"(?:картинк|изображени|иллюстрац|постер|плакат|баннер|логотип|эмблем|маскот|талисман|"
    r"макет|мем\b|мемы|мемов|мема|визитк|стикерпак|"
    r"аватар|обложк|превью|обои|заставк|стикер|открытк|наклейк|комикс|скринсейвер|эскиз|"
    r"набросок|карикатур|image|picture|illustration|poster|logo|banner|avatar|wallpaper|"
    r"sticker|mockup|sketch|drawing|cartoon|mascot|flyer|emblem|emblems|thumbnail|thumbnails)",
    re.IGNORECASE,
)
# Глагол просьбы: он превращает название артефакта в заказ.
_IMAGE_REQUEST_VERB_RE = re.compile(
    r"(?:^|[\s,.!?])(?:хочу|хочешь|хотелось|хотел\s+бы|нужен|нужна|нужно|надо|можно|"
    r"дай|давай|сдела\w*|созда\w*|придума\w*|накида\w*|сгенери\w*|нарис\w*|увид\w*|получи\w*|"
    r"want|need|give|get|make|create|draw|design|generate|render|produce|see|please)\b"
    r"|(?:can|could|will)\s+you\b",
    re.IGNORECASE,
)
# Артефакт в роли детали: «книга с красивой обложкой», «дом с большим
# макетом». Заказать рисование так не просят (ревью 10.11.2026). Группу артефакта
# помечаем: «сделай картинку с логотипом» — это заказ картинки, а не детали.
_IMAGE_PART_RE = re.compile(
    r"\w+\s+(?:с|со|with)\s+(?:\w+\s+){0,2}"
    r"(?P<part>обложк|превью|логотип|иконк|стикер|макет|постер|плакат|баннер|эмблем|"
    r"иллюстрац|картинк|изображени|аватар|stickers?|logos?|banner|poster|cover)\w*",
    re.IGNORECASE,
)
# Артефакт первым словом — implicit заказ, как в ChatGPT («poster for a gig»,
# «логотип кофейни»). До этого такие фразы терялись: глагол просьбы в них есть
# только по смыслу (ревью 10.11.2026).
_IMAGE_HEAD_ARTIFACT_RE = re.compile(
    r"^(?:картинк|изображени|иллюстрац|постер|плакат|баннер|логотип|эмблем|маскот|"
    r"талисман|аватар|обложк|обои|заставк|стикер|стикерпак|наклейк|открытк|комикс|"
    r"мем|макет|эскиз|набросок|карикатур|превью|визитк|скринсейвер|эпиграф|"
    r"image|picture|poster|logo|banner|avatar|wallpaper|sticker|mockup|sketch|"
    r"drawing|cartoon|mascot|flyer|thumbnail|emblem|icon)\w*\b",
    re.IGNORECASE,
)
# Жалоба на картинку или утверждение о ней — не просьба нарисовать: сюда же
# «обложка альбома была лучшей» и «image not loading» (ревью 10.11.2026).
_IMAGE_COMPLAINT_RE = re.compile(
    r"\b(?:не|ні|немає|ошибк\w*|error|бит\w*|сломан\w*|broken|failed)\b"
    r"|\bне\s+(?:работает|грузится|открыв\w*|видн\w*|загруз\w*)\b"
    r"|\bnot\s+\w+ing\b|\bdoesn'?t\s+work\b|\bcan'?t\s+(?:see|open|load)\b"
    r"|\b(?:был[аио]?|были|стал[аи]?|выглядит|нравится|понравил[аи]сь|was|were|is|are)\b",
    re.IGNORECASE,
)
# Указание на уже существующий кадр: тогда это правка, а не новая генерация.
_IMAGE_EXISTING_RE = re.compile(
    r"\b(?:этот|этого|этому|этим|эта|этой|эту|эти|этих|тот|того|та|ту|те|такую|такой|"
    r"её|ее|неё|ней|this|that|previous|earlier|same)\b|\b(?:прежн|прошл)",
    re.IGNORECASE,
)
# Императив рисования в любом месте фразы: «кот в шляпе, нарисуй».
# generate/render/сгенерируй здесь не нужны: «сгенерируй отчёт» и «generate a
    # summary» — текстовые задачи (ревью 10.11.2026). Они остаются только в
    # префиксах с артефактным словом («generate an image», «сгенери картинку»).
_DRAW_IMPERATIVE_ANYWHERE_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё])(нарисуй-ка|нарисуй|нарисуйте|изобрази|"
    r"draw|sketch|illustrate|render|paint)\b",
    re.IGNORECASE,
)
# Фраза, начинающаяся с артефакта: «picture of a cat», «логотип для кофейни».
# Только «артефакт + предлог сюжета»: «picture of a cat», «логотип для
    # кофейни». Голое слово («картинка не загружается») — не заказ (ревью 10.11.2026).
_IMAGE_LED_RE = re.compile(
    r"^(?:картинка|картинку|картину|изображение|иллюстрация|иллюстрацию|постер|плакат|"
    r"баннер|логотип|эмблема|маскот|аватар|аватарка|обложка|обои|стикер|"
    r"image|picture|photo|illustration|poster|logo|banner|avatar|wallpaper|sticker|mockup)"
    r"\s+(?:of|for|with|с|со|для|на|в|про)\b",
    re.IGNORECASE,
)


# Местоименная просьба: «нарисуй это», «изобрази то, что я описал». Без
# разрешения по истории в модель уходило слово «это» — и рисовался мусор.
_DRAW_ANAPHORA_RE = re.compile(
    r"^(?:это|то|это\s+красиво|то\s+же\s+самое|такую\s+же|такой\s+же|такое\s+же|такое|"
    r"the\s+same|this|that|it)\b"
    r"|\b(?:то|это),?\s+что\s+я\b"
    r"|\b(?:this|that)\s+(?:you\s+)?described\b",
    re.IGNORECASE,
)
# Слова, которые ничего не добавляют к просьбе: «верни предыдущий вариант ок».
_POLITE_TAIL_RE = re.compile(
    r"\b(?:пожалуйста|please|ок|окей|хорошо|it|this|то|это)\b",
    re.IGNORECASE,
)
# Просьба нарисовать без названия артефакта: «хочу увидеть кота на луне»,
# «imagine a city on the moon». Одна эта фраза картинку не заказывает — она
# только оправдывает один короткий вопрос модели.
_DRAW_VISUAL_SIGNAL_RE = re.compile(
    r"\b(?:увидеть|увидим|представь|представь\s+себе|визуализируй|нарисуй\s+нам)\b"
    r"|\b(?:imagine|visualise|visualize|picture\s+this)\b",
    re.IGNORECASE,
)


def _is_anaphoric_draw_request(content: str) -> bool:
    """Тело просьбы — указание на то, что уже обсуждалось. Ищем от начала:
    «кота, это что я люблю» — конкретный сюжет, а не местоимение (ревью 10.11.2026)."""
    text = (content or "").strip()
    return bool(_DRAW_ANAPHORA_RE.match(text))


def _is_bare_trigger_request(rest: str) -> bool:
    """Пусто или только вежливые слова: «верни предыдущий вариант, пожалуйста» —
    это всё ещё возврат, а не просьба о тексте (ревью 10.11.2026)."""
    text = (rest or "").strip()
    for _ in range(3):
        stripped = _DRAW_FILLER_RE.match(text.lower())
        cut = len(stripped.group(0)) if stripped else 0
        text = _POLITE_TAIL_RE.sub("", text[cut:]).strip(" ,.!?:;-")
        if not cut:
            break
    return not re.search(r"\w", text, re.UNICODE)


def _has_visual_signal(text_lower: str) -> bool:
    """Вопрос («как представить себе...»), команда и ссылка на сайт — не просьба
    рисовать: сигнал считаем только у чистого утвердительного текста."""
    if _DRAW_BLOCK_LEAD_RE.search(text_lower) or text_lower.startswith("/"):
        return False
    # «представь краткое содержание https://…» — чтение сайта, а не кадр;
    # вопрос про картинку — тоже не заказ (ревью 10.11.2026).
    if ("?" in text_lower or "？" in text_lower
            or "http://" in text_lower or "https://" in text_lower):
        return False
    return bool(_DRAW_VISUAL_SIGNAL_RE.search(text_lower))


def _strip_trigger_fillers(text_lower: str) -> tuple[str, int]:
    """(текст без обвязки, длина снятого префикса): «ну нарисуй кота» и
    «мне, пожалуйста, нарисуй кота» — та же просьба, что «нарисуй кота».
    Снимаем только известные слова-связки и только в начале, максимум четыре."""
    m = _DRAW_FILLER_RE.match(text_lower)
    cut = len(m.group(0)) if m else 0
    return text_lower[cut:], cut


def _match_trigger_with_fillers(text_lower: str, prefixes) -> tuple[str | None, int]:
    """(триггер, сколько символов снято). Триггер в чистом начале приоритетнее:
    прежние маршруты не двигаются. Дальше перебираем границы обвязки — «ну ещё
    вариант» начинается со второго слова, и срез обязан быть точно по нему.
    Без найденного триггера срез не отдаём: иначе «please draw a cat» теряло
    «please draw» дважды и в модель уходило «in a hat» (ревью 10.11.2026)."""
    for cut in _filler_offsets(text_lower):
        hit = _match_trigger_prefix(text_lower[cut:], prefixes)
        if hit:
            return hit, cut
    return None, 0


def _is_new_artifact_request(text: str) -> bool:
    """«сделай картинку кота» — новая генерация, «сделай эту картинку ярче» —
    правка: указание на существующий кадр перевешивает слабый глагол."""
    if _DRAW_BLOCK_LEAD_RE.search(text) or not _IMAGE_ARTIFACT_RE.search(text):
        return False
    if _IMAGE_EXISTING_RE.search(text):
        return False
    head = bool(_IMAGE_HEAD_ARTIFACT_RE.match(_strip_trigger_fillers(text)[0]))
    verb = bool(_IMAGE_REQUEST_VERB_RE.search(text))
    # Без глагола просьбы артефакт заказывает кадр только стоя в начале фразы,
    # и то не в жалобе («картинка не загружается»).
    if not verb:
        return head and not _IMAGE_COMPLAINT_RE.search(text)
    # С глаголом — ловим только «артефакт как деталь чужого предмета»: если хоть
    # одно артефактное слово стоит вне связки «книга с обложкой», это заказ
    # (ревью 10.11.2026: «сделай картинку с логотипом кота»).
    parts = [m.span("part") for m in _IMAGE_PART_RE.finditer(text)]
    for m in _IMAGE_ARTIFACT_RE.finditer(text):
        if not any(a <= m.start() < b for a, b in parts):
            return True
    return False


def _match_draw_intent(text_lower: str, text_original: str) -> tuple[str, str]:
    """(вид, описание): "draw" — просьба нарисовать без триггера в начале фразы.
    Пустой вид — обычные маршруты (текст, правка, повтор) не трогаем."""
    core_start = len(_DRAW_FILLER_RE.match(text_lower).group(0))
    core = text_lower[core_start:]
    # «ну покажи картинку» — показ, а не генерация: лид проверяем после обвязки.
    # Вопрос («какой логотип лучше выбрать?») и жалоба («image not loading»)
    # заказом не считаются (ревью 10.11.2026).
    if _DRAW_BLOCK_LEAD_RE.search(core) or "?" in core or "？" in core:
        return "", ""
    if _IMAGE_COMPLAINT_RE.search(core):
        return "", ""

    if _match_trigger_prefix(core, DRAW_TRIGGER_PREFIXES):
        # Обычный префиксный путь: он уже обработан выше по коду, здесь не дублируем.
        return "", ""
    if _is_new_artifact_request(text_lower):
        return "draw", text_original.strip()
    m = _DRAW_IMPERATIVE_ANYWHERE_RE.search(text_original[core_start:])
    if m:
        rest = re.sub(
            r"\s{2,}", " ",
            (text_original[core_start:core_start + m.start()] + " "
             + text_original[core_start + m.end():]),
        ).strip(" ,.!?:;-")
        if rest:
            return "draw", rest
    if _IMAGE_LED_RE.match(core):
        return "draw", text_original.strip()
    return "", ""

# «Нарисуй X в стиле этого фото»: генерация с референсом стиля (как в ChatGPT).
# Срабатывает только когда рядом есть фото — иначе это обычная генерация.
_STYLE_PHRASES = (
    "в стиле", "по образцу",
    "как на этом фото", "как на этой картинке", "как на этом изображении",
)
_STYLE_RE = re.compile(r"in the style of|like this (?:photo|image)", re.IGNORECASE)


def _wants_style_reference(text: str) -> bool:
    """Просили перенести стиль с приложенного фото, а не просто нарисовать."""
    low = (text or "").lower()
    return any(phrase in low for phrase in _STYLE_PHRASES) or bool(_STYLE_RE.search(low))


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
    # EN: /start обещает "read this out loud" (найдено внешним аудитом).
    "read this out loud", "read it out loud", "read out loud", "read aloud",
    "voice this", "voice it", "speak this", "say this aloud", "narrate this",
]

def _match_trigger_prefix(text_lower: str, prefixes: list[str]) -> str | None:
    """Самый длинный триггер в начале строки: «нарисуй» стоит раньше «нарисуй-ка» и
    обрезал «нарисуй-ка кота» до «ка кота» (ревью 10.11.2026). Нужна граница слова,
    иначе "прочтите"/"нарисуйка" дадут битую генерацию."""
    best: str | None = None
    for prefix in prefixes:
        if not text_lower.startswith(prefix):
            continue
        rest = text_lower[len(prefix):]
        if rest and rest[0].isalpha():
            continue
        if best is None or len(prefix) > len(best):
            best = prefix
    return best

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


# Чаты, записанные до 30.10.2026, хранят внутри реплик одноразовую службу: блок
# текущей даты и выдачу поиска (выборка бэкапа: 19 реплик из 210, до 68% объёма
# user-контекста). Плейсхолдер вместо ответа тоже осел в истории.
_HISTORY_SERVICE_RE = re.compile(
    r"\s*\[Context: today[^\]]*\]|\s*\[Web search (?:snippets|results)[\s\S]*$", re.IGNORECASE)


def _clean_history_entry(item: dict) -> dict | None:
    """Реплика истории без служебных блоков; None — выбросить целиком."""
    text = item.get("content")
    if not isinstance(text, str):
        return item
    cleaned = _HISTORY_SERVICE_RE.sub("", text).strip()
    if not cleaned or _is_empty_like(cleaned):
        # Медиа без подписи — законная реплика: её метку [фото] строит lumen_memory.
        if item.get("media"):
            return {**item, "content": ""}
        return None
    if cleaned == text:
        return item
    return {**item, "content": cleaned}

# Только явные существительные медиа: общие "это/покажи" тянули чужое фото и давали галлюцинации.
# Именованные группы — чтобы понять КАКОЙ тип спросили (регрессия 18.08.2026: "покажи стикер" описывал чужое фото).
_MEDIA_REFERENCE_RE = re.compile(
    r"\b(?:"
    r"(?P<sticker>стикер\w*)|"
    # Отрицательные хвосты: «видеокарта», «аудиокнига», «фотосинтез»,
    # «документация», «файловая система» — это не вложения (аудит 29.10.2026).
    r"(?P<video>видео(?!карт|звон|игр|плеер|редактор|наблюден|курс|связ)\w*|видос\w*|ролик\w*|клип\w*|кружок\w*|gif\w*|гиф\w*)|"
    r"(?P<audio>аудио(?!книг|плеер)\w*|голосов\w*|войс\w*)|"
    r"(?P<photo>фото(?!синтез|н\b|ны\b|аппарат|элемент|эффект)\w*|снимок\w*|изображени\w*|скрин\w*|скриншот\w*|картинк\w*)|"
    r"(?P<document>документ(?!аци|овед|альн)\w*|pdf|пдф|файл(?!ов)\w*)"
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
    "игрушку": "games",
    "movie": "film", "movies": "film", "film": "film", "films": "film",
    "series": "series", "show": "series", "shows": "series",
    "music": "music", "song": "music", "songs": "music",
    "track": "music", "tracks": "music",
    "book": "books", "books": "books",
    "game": "games", "games": "games",
}
# Игровые формы — только целиком с двух сторон: префикс "игр" ловил глаголы
# ("кто играет сегодня" — не запрос игры). Инфинитивы оставляем: "во что поиграть" — игры.
_PICK_EXACT_TOPICS: dict[str, str] = {
    "игра": "games", "игры": "games", "игру": "games", "игрой": "games",
    "игре": "games", "игр": "games", "играть": "games", "поиграть": "games",
}
_PICK_MAX_LEN = 60

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
    for topic, scenario in _PICK_EXACT_TOPICS.items():
        if re.search(r"(?<!\w)" + re.escape(topic) + r"(?!\w)", text):
            return scenario
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
