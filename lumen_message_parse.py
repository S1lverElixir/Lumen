"""lumen_message_parse.py parses incoming messages: links, trigger phrases
/draw and /tts without a slash, reply markers, media references, and the
\"no file\" marker. Pure functions and configuration, with no dependency on the
bot runtime.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from lumen_errors import _is_empty_like

_TIKTOK_RE = re.compile(r"(?:^|\.)tiktok\.com$")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}

def extract_url(text: str) -> str | None:
    """The first link in the text. The second and later links are ignored: TikTok/YouTube paths take one link (see match handling in message_core)."""
    m = re.search(r"https?://[^\s]+", text)
    if not m:
         return None
    url = m.group(0)
    # Trim the closing bracket only when unbalanced; otherwise the address
    # "…/Doom_(1993_video_game)" lost the parenthesis and could not be read (external audit 16.11.2026).
    while url and url[-1] in ".,!?;:>'\"])":
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url

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

# Triggers use startswith only: otherwise "how to draw..." would falsely start generation.
DRAW_TRIGGER_PREFIXES = [
    "сгенерируй картинку", "сгенерируй мне картинку", "сгенерируй изображение",
    "сгенерируй мне изображение", "создай картинку", "создай мне картинку",
    "создай изображение", "создай мне изображение", "нарисуй картинку", "нарисуй изображение",
    "нарисуй мне", "нарисуй", "нарисуйте", "изобрази картинку", "изобрази", "нарисуй-ка",
    "сгенери картинку", "сгенери изображение", "можешь нарисовать", "можешь нарисовать мне",
    "ты можешь нарисовать", "можешь нарисовать мне", "сможешь нарисовать", "можете нарисовать",
    "не мог бы ты нарисовать", "мог бы ты нарисовать", "can you draw",
    "could you draw", "please draw", "i want you to draw",
    # EN: /start promises 'draw a cat'; fix it (found by an external audit: only Russian had it).
    "generate an image", "generate me an image", "create an image", "draw a picture",
    "draw me", "draw an", "draw a", "paint me", "paint", "sketch",
]
# Image-edit verbs: trigger only when a reference exists — an attachment,
# a reply to a photo or (for strong verbs) a recent photo by the author. Without an image
# the text goes to an ordinary model, so 'change the world' draws nothing.
EDIT_TRIGGER_PREFIXES = [
    "отредактируй", "переделай", "перерисуй", "измени", "поменяй", "замени",
    "дорисуй", "подправь", "подкорректируй", "убери", "добавь", "сделай",
    "edit", "modify", "change", "replace", "redraw", "retouch",
    "remove", "add", "make the", "make it", "make them", "turn the", "swap the",
]
# Weak verbs ("make/remove/add") are meaningful only with an explicit reference
# to a picture (reply/attachment): otherwise "make me coffee" would become an edit.
EDIT_TRIGGER_IMPLICIT = [
    "отредактируй", "переделай", "перерисуй", "измени", "поменяй", "замени",
    "дорисуй", "подправь", "подкорректируй",
    "edit", "modify", "change", "replace", "redraw", "retouch",
]
# Regeneration: "another version" after an image repeats the most recent description
# (freshness is checked by _fresh_generation_ref). Without a fresh generation, this is ordinary text,
# so “repeat” is not included here: it too often means “repeat the answer.”
REPEAT_TRIGGER_PREFIXES = [
    "ещё вариант", "еще вариант", "ещё варианты", "еще варианты", "другой вариант",
    "другие варианты", "новый вариант", "ещё картинку", "еще картинку",
    "ещё одну картинку", "еще одну картинку",
    "другую картинку",
    "another version", "another variant", "regenerate",
]
# Reverting to the previous version: the photo is already at Telegram; no provider is needed
# and the daily slot is not consumed (parity with Undo/Redo in ChatGPT, 10.11.2026).
REVERT_TRIGGER_PREFIXES = [
    "верни предыдущий вариант", "верни прошлый вариант", "верни предыдущую картинку",
    "верни прошлую картинку", "верни предыдущее изображение", "предыдущий вариант",
    # A resolution on an empty tail ("14.2") also allows short forms:
    # previously "return the previous one" and "return the picture" silently went to the text model.
    "верни предыдущий", "верни прошлый", "верни картинку", "верни фото",
    "верни изображение", "верни как было", "верни то", "previous version",
    "прошлый вариант", "отмени правку", "отмени последнее изменение",
    "верни назад", "back to the previous version", "undo",
]
REDO_TRIGGER_PREFIXES = [
    "верни обратно", "верни последнюю версию", "верни как стало", "повтори правку",
    "redo",
]
# "Draw 2 options" as in ChatGPT: the option count comes from the prompt, 2-4.
# Search only near the word option/picture/image, not for any number.
_VARIANTS_RE = re.compile(
    r"(?<!\d)([2-4])\s*(?:вариант\w*|картинк\w*|изображени\w*|верси\w*|versions?|variants?)(?!\w)",
    re.IGNORECASE,
)
_VARIANTS_WORDS: dict[str, int] = {"два": 2, "две": 2, "три": 3, "четыре": 4, "two": 2, "three": 3, "four": 4}


def _split_image_variants(text: str) -> tuple[int, str]:
    """(number of options, text without the quantity phrase): “2 cat options” ->
    (2, “cat”), so the number does not reach the model and the repeat (“another option”)."""
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


_VARIANTS_STRIP_RE = re.compile(
    r"(?<!\d)(?:[2-4]\s*)?(?:вариант\w*|variants?)(?!\w)", re.IGNORECASE,
)
_VARIANTS_WORD_RE = re.compile(
    r"(?<!\w)(?:два|две|три|четыре|two|three|four)\s+(?:вариант\w*|variants?)(?!\w)",
    re.IGNORECASE,
)


def _strip_variants_phrase(text: str) -> str:
    """Removes only the phrase about options: \"3 versions\" and \"3 images\" in the resolver description are part of the story, so they are left alone (pass 5, 17.11.2026)."""
    src = text or ""
    cleaned = _VARIANTS_STRIP_RE.sub(" ", src)
    cleaned = _VARIANTS_WORD_RE.sub(" ", cleaned)
    if cleaned == src:
        # Nothing was removed — return the text byte for byte, including the final period.
        return src
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return re.sub(r"(?:,\s*)+", ", ", cleaned).strip(" ,")
    return re.sub(r"\s{2,}", " ", cleaned).strip(" ,.-")


# Wake word in groups: "Lumen, ..." instead of an @mention
# Rules against false positives (review 18.11.2026):
# - only at the start of a message (after short address words like
# "hey"/"listen"/"okay");
#   - regular form: name + punctuation ("," ":" "—" "!" "?" "." + a space);
#  - the spaced form is a heuristic: block third-person forms ("can," "good");
# - a dot is a delimiter only before a space or the end: 'lumen.example.com' is not a name;
#   - do not touch a name inside a word, mid-phrase, an @mention, or a URL.
_WAKE_PARTICLE_RE = re.compile(
    r"^(?:эй|слушай|окей|okay|hey)\s*[,.!—–-]?\s+", re.IGNORECASE,
)
_WAKE_WEAK_PARTICLE_RE = re.compile(
    r"^(?:а|ну|ладно)\s*[,.!—–-]\s*", re.IGNORECASE,
)
_WAKE_COPULA_RE = re.compile(
    r"^(?:это|is|является|был|была|было|были)(?![А-Яа-яA-Za-z])", re.IGNORECASE,
)
# Third person in spaced form: "Lumen can...", "Lumen is good...".
_WAKE_THIRD_PERSON_RE = re.compile(
    r"^(?:умеет|может|хорош\w*|лучш\w*|работает|отвечает|жив[её]т|приш[её]л|"
    r"упал\w*|стоит|любит|знает|понимает|принадлежит|не\s+бот|бот\b|"
    r"is|was|can|works|helps)(?![А-Яа-яA-Za-z])",
    re.IGNORECASE,
)


def strip_group_wake_word(text: str, words: tuple[str, ...] = ("люмен", "lumen")) -> tuple[bool, str]:
    """(whether the wake word matched, text without it)."""
    src = (text or "").strip()
    if not src or src.startswith("/"):
        return False, text
    rest = src
    for _ in range(2):
        m = _WAKE_PARTICLE_RE.match(rest) or _WAKE_WEAK_PARTICLE_RE.match(rest)
        if not m:
            break
        rest = rest[m.end():]
    rest = rest.lstrip("\"'«“(")
    low = rest.lower()
    for word in words:
        w = (word or "").strip().lower()
        if not w or not low.startswith(w):
            continue
        after = rest[len(w):]
        if after[:1] and (after[0].isalnum() or after[0] in "_@"):
            continue
        after = after.lstrip().lstrip("\"'»”)")
        punct_delim = False
        if after[:1] in ("—", "–", "-"):
            # An em dash indicates address, but “Lumen is a bot” remains small talk: check the copula.
            after = after[1:].lstrip()
        elif after[:1] in (",", ":", ";", "!", "?"):
            after = after[1:].lstrip()
            punct_delim = True
        elif after[:1] == ".":
            # "lumen.example.com" is not an address; "Lumen. Hello" is an address.
            nxt = after[1:2]
            if nxt and not nxt.isspace():
                continue
            after = after[1:].lstrip()
            punct_delim = True
        elif after[:1].isspace():
            after = after.lstrip()
        if not after:
            continue
        if not punct_delim:
            # Space-separated form: remove the obvious third person and the copula.
            if _WAKE_COPULA_RE.match(after) or _WAKE_THIRD_PERSON_RE.match(after):
                return False, text
        after = re.sub(r"^[^\w/]+", "", after, flags=re.UNICODE)
        if not after:
            continue
        if not re.search(r"[0-9A-Za-zА-Яа-яЁё]", after):
            continue
        return True, after
    return False, text


def _detect_image_variants(text: str) -> int:
    """How many variants were requested: 1 — regular generation, 2-4 — multiple."""
    return _split_image_variants(text)[0]



# ChatGPT parity (10.11.2026): it draws on any request where
# the meaning of "make a picture," not the first word. A person writes "come on, draw
# a cat", "a cat in a hat, draw it", "I want a picture with a cat", "I need a logo
# cafes", "make me an image of a cat". Meanwhile, "how to draw a cat"
# and auxiliary verbs ("show the picture", "save", "translate") with an image
# should not become
# An opening question or service verb: before those, do not treat it as a request to draw.
_DRAW_BLOCK_LEAD_RE = re.compile(
    r"^(?:как|почему|зачем|что|кто|где|когда|сколько|можно|нужно\s+ли|объясни|расскажи|"
    r"покажи|сохрани|переведи|удали|найди|открой|закрой|отправь|пришли|погоди|сравни|"
    r"how|why|what|who|where|when|explain|show|save|translate|delete|find|open|send)\b",
    re.IGNORECASE,
)
    # Wrapper before the verb: "come on, draw", "please draw for me", "can you..."
    # One filler word removed: cut the chain “well, another option” at word boundaries,
    # because "another" is both a connector and the first word of the repeat trigger
    # (review 10.11.2026).
_DRAW_FILLER_ONE_RE = re.compile(
    r"^(?:(?:ну|давай|а|и|вот|мне|пожалуйста|ещё|еще|тоже|также|сначала|для\s+начала|"
    r"please|pls|also|just)\b[,\s]*)",
    re.IGNORECASE,
)


def _filler_offsets(text_lower: str) -> list[int]:
    """Possible starts of a request: 0 and the position after each removed word."""
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
# The artifact the person asks to create (in ChatGPT this also includes templates: a poster, logo, or cover).
_IMAGE_ARTIFACT_RE = re.compile(
    r"(?:картинк|изображени|иллюстрац|постер|плакат|баннер|логотип|эмблем|маскот|талисман|"
    r"макет|мем\b|мемы|мемов|мема|визитк|стикерпак|"
    r"аватар|обложк|превью|обои|заставк|стикер|открытк|наклейк|комикс|скринсейвер|эскиз|"
    r"набросок|карикатур|image|picture|illustration|poster|logo|banner|avatar|wallpaper|"
    r"sticker|mockup|sketch|drawing|cartoon|mascot|flyer|emblem|emblems|thumbnail|thumbnails)",
    re.IGNORECASE,
)
# Request verb: it turns an artifact name into an order.
_IMAGE_REQUEST_VERB_RE = re.compile(
    r"(?:^|[\s,.!?])(?:хочу|хочешь|хотелось|хотел\s+бы|нужен|нужна|нужно|надо|можно|"
    r"дай|давай|сдела\w*|созда\w*|придума\w*|накида\w*|сгенери\w*|нарис\w*|увид\w*|получи\w*|"
    r"want|need|give|get|make|create|draw|design|generate|render|produce|see|please)\b"
    r"|(?:can|could|will)\s+you\b",
    re.IGNORECASE,
)
# Artifact as a detail: "a book with a beautiful cover", "a house with a large
# layout". Asking for drawing that way is not how people request it (review 10.11.2026). The artifact group
# we flag it: "make an image with a logo" is an image order, not a detail.
_IMAGE_PART_RE = re.compile(
    r"\w+\s+(?:с|со|with)\s+(?:\w+\s+){0,2}"
    r"(?P<part>обложк|превью|логотип|иконк|стикер|макет|постер|плакат|баннер|эмблем|"
    r"иллюстрац|картинк|изображени|аватар|stickers?|logos?|banner|poster|cover)\w*",
    re.IGNORECASE,
)
# An artifact as the first word is an implicit order, as in ChatGPT ('poster for a gig',
# "coffee shop logo"). Before this, such phrases were lost: they contain a request verb
# only by meaning (review 10.11.2026).
_IMAGE_HEAD_ARTIFACT_RE = re.compile(
    r"^(?:картинк|изображени|иллюстрац|постер|плакат|баннер|логотип|эмблем|маскот|"
    r"талисман|аватар|обложк|обои|заставк|стикер|стикерпак|наклейк|открытк|комикс|"
    r"мем|макет|эскиз|набросок|карикатур|превью|визитк|скринсейвер|эпиграф|"
    r"image|picture|poster|logo|banner|avatar|wallpaper|sticker|mockup|sketch|"
    r"drawing|cartoon|mascot|flyer|thumbnail|emblem|icon)\w*\b",
    re.IGNORECASE,
)
# A complaint about an image or a statement about it is not a drawing request; this also covers
# "the album cover was better" and "image not loading" (review 10.11.2026).
_IMAGE_COMPLAINT_RE = re.compile(
    r"\b(?:не|ні|немає|ошибк\w*|error|бит\w*|сломан\w*|broken|failed)\b"
    r"|\bне\s+(?:работает|грузится|открыв\w*|видн\w*|загруз\w*)\b"
    r"|\bnot\s+\w+ing\b|\bdoesn'?t\s+work\b|\bcan'?t\s+(?:see|open|load)\b"
    r"|\b(?:был[аио]?|были|стал[аи]?|выглядит|нравится|понравил[аи]сь|was|were|is|are)\b",
    re.IGNORECASE,
)
# A reference to an existing frame: this is an edit, not a new generation.
_IMAGE_EXISTING_RE = re.compile(
    r"\b(?:этот|этого|этому|этим|эта|этой|эту|эти|этих|тот|того|та|ту|те|такую|такой|"
    r"её|ее|неё|ней|this|that|previous|earlier|same)\b|\b(?:прежн|прошл)",
    re.IGNORECASE,
)
    # A drawing imperative anywhere in the phrase: “cat in a hat, draw”.
    # generate/render vocabulary (including inflected forms) is not needed here: "generate a report" and "generate a
    # summary" — text tasks (review 10.11.2026). They remain only in
    # prefixes containing an artifact word ('generate an image', 'generate a picture').
_DRAW_IMPERATIVE_ANYWHERE_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё])(нарисуй-ка|нарисуй|нарисуйте|изобрази|"
    r"draw|sketch|illustrate|render|paint)\b",
    re.IGNORECASE,
)
    # A phrase beginning with an artifact: "picture of a cat", "a logo for a coffee shop."
    # Only "artifact + subject preposition": "picture of a cat", "a logo for
    # coffee shops". A bare word ("the image will not load") is not an order (review 10.11.2026).
_IMAGE_LED_RE = re.compile(
    r"^(?:картинка|картинку|картину|изображение|иллюстрация|иллюстрацию|постер|плакат|"
    r"баннер|логотип|эмблема|маскот|аватар|аватарка|обложка|обои|стикер|"
    r"image|picture|photo|illustration|poster|logo|banner|avatar|wallpaper|sticker|mockup)"
    r"\s+(?:of|for|with|с|со|для|на|в|про)\b",
    re.IGNORECASE,
)


# Pronominal request: "draw this," "depict what I described." Without
# in the model context was the word "this", and it drew garbage.
_DRAW_ANAPHORA_RE = re.compile(
    r"^(?:это|то|это\s+красиво|то\s+же\s+самое|такую\s+же|такой\s+же|такое\s+же|такое|"
    r"the\s+same|this|that|it)\b"
    r"|\b(?:то|это),?\s+что\s+я\b"
    r"|\b(?:this|that)\s+(?:you\s+)?described\b",
    re.IGNORECASE,
)
# Words that add nothing to the request: "bring back the previous version, okay".
_POLITE_TAIL_RE = re.compile(
    r"\b(?:пожалуйста|please|ок|окей|хорошо|it|this|то|это)\b",
    re.IGNORECASE,
)
# A request to draw without an artifact name: “I want to see a cat on the moon,”
# "imagine a city on the moon". That phrase alone does not order a picture — it
# only justifies one short question to the model.
_DRAW_VISUAL_SIGNAL_RE = re.compile(
    r"\b(?:увидеть|увидим|представь|представь\s+себе|визуализируй|нарисуй\s+нам)\b"
    r"|\b(?:imagine|visualise|visualize|picture\s+this)\b",
    re.IGNORECASE,
)


def _is_anaphoric_draw_request(content: str) -> bool:
    """The body of the request refers to something already discussed. Search from the
    beginning: \"the cat, that is what I love\" is a specific subject, not a pronoun
    (review 10.11.2026)."""
    text = (content or "").strip()
    return bool(_DRAW_ANAPHORA_RE.match(text))


def _is_bare_trigger_request(rest: str) -> bool:
    """Empty or polite words only: "return the previous version, please" is still a return,
    not a request for text (review 10.11.2026).
    """
    text = (rest or "").strip()
    for _ in range(3):
        stripped = _DRAW_FILLER_RE.match(text.lower())
        cut = len(stripped.group(0)) if stripped else 0
        text = _POLITE_TAIL_RE.sub("", text[cut:]).strip(" ,.!?:;-")
        if not cut:
            break
    return not re.search(r"\w", text, re.UNICODE)


def _has_visual_signal(text_lower: str) -> bool:
    """A question (\"how to picture...\"), command, and website link are not requests to draw: count the signal only for purely affirmative text."""
    if _DRAW_BLOCK_LEAD_RE.search(text_lower) or text_lower.startswith("/"):
        return False
    # "give a brief summary of https://…" — reading a site, not generating a frame;
    # a question about an image is also not an order (review 10.11.2026).
    if ("?" in text_lower or "？" in text_lower
            or "http://" in text_lower or "https://" in text_lower):
        return False
    return bool(_DRAW_VISUAL_SIGNAL_RE.search(text_lower))


def _strip_trigger_fillers(text_lower: str) -> tuple[str, int]:
    """(text without filler, length of the removed prefix): “come on, draw a cat” and “please draw me a cat” are the same request as “draw a cat.” Only known filler words are removed, only at the start, at most four."""
    m = _DRAW_FILLER_RE.match(text_lower)
    cut = len(m.group(0)) if m else 0
    return text_lower[cut:], cut


# "draw a table of the results" — a request to write a table, not make a picture
# (external audit 16.11.2026). A weak English verb without a visual object
# at the start of the remainder goes to the regular model; Russian triggers are not restricted.
_WEAK_DRAW_PREFIXES = frozenset(("draw a", "draw an", "draw me", "paint", "paint me", "sketch"))
# Any trigger ending in a drawing verb is also weak: "can you draw" and
# "please draw" otherwise bypassed the check (fix verification 16.11.2026).
_WEAK_DRAW_LAST_WORDS = frozenset(("draw", "paint", "sketch"))
# Textual artifacts are the model's answer in words. "summary card/poster/..." remain
# images: this is a visual object, not a description (fix verification 16.11.2026).
_DRAW_TEXTUAL_TAIL_RE = re.compile(
    r"^(?:a |an |the )?(?:table|list|comparison|conclusion|summary)\b"
    r"(?!\s+(?:card|poster|infographic|chart|diagram|image|picture))", re.IGNORECASE)


def _draw_trigger_is_visual(trigger: str, rest: str) -> bool:
    """A weak English trigger draws only if an image object follows.
    A modifier (\"table with a vase\") does not help: \"table of the results\" and
    \"table with the results\" mean the same thing, while an extra drawn answer burns
    a personal slot; someone asking about a rare piece of furniture will say \"photo of a table\"."""
    words = trigger.split()
    if trigger not in _WEAK_DRAW_PREFIXES and not (words and words[-1] in _WEAK_DRAW_LAST_WORDS):
        return True
    tail = rest.lstrip(" ,:;\t")
    return not _DRAW_TEXTUAL_TAIL_RE.match(tail)


# "read the instructions and explain what to do" — a request to analyze, not to read aloud
# (external audit 16.11.2026). Bare “read/read aloud” without a retelling verb
# remain speech: "read this paragraph aloud" means TTS.
_TTS_BARE_READ = frozenset(("прочти", "прочтите", "зачитай", "зачитайте"))
_TTS_EXPLAIN_RE = re.compile(r"\b(?:объясни|объясните|расскажи|расскажите|перескажи|перескажите)\b")


def _tts_trigger_is_speech(trigger: str, rest: str) -> bool:
    if trigger not in _TTS_BARE_READ:
        return True
    return not _TTS_EXPLAIN_RE.search(rest)


def _match_trigger_with_fillers(text_lower: str, prefixes) -> tuple[str | None, int]:
    """(trigger, number of characters removed). A trigger at the exact beginning takes
    priority so existing routes do not change. We then try wrapper boundaries —
    \"well, another version\" starts at the second word, and the slice must match it exactly.
    If no trigger is found, we do not return the slice: otherwise \"please draw a cat\"
    lost \"please draw\" twice and \"in a hat\" reached the model (10.11.2026 review)."""
    for cut in _filler_offsets(text_lower):
        hit = _match_trigger_prefix(text_lower[cut:], prefixes)
        if hit:
            return hit, cut
    return None, 0


def _is_new_artifact_request(text: str) -> bool:
    """“make a picture of a cat” — new generation; “make this picture brighter” — edit: a reference to an existing image outweighs the weak verb."""
    if _DRAW_BLOCK_LEAD_RE.search(text) or not _IMAGE_ARTIFACT_RE.search(text):
        return False
    if _IMAGE_EXISTING_RE.search(text):
        return False
    head = bool(_IMAGE_HEAD_ARTIFACT_RE.match(_strip_trigger_fillers(text)[0]))
    verb = bool(_IMAGE_REQUEST_VERB_RE.search(text))
    # Without a verb, an artifact request orders a frame only at the start of the phrase,
    # and even then not in a complaint ("the image will not load").
    if not verb:
        return head and not _IMAGE_COMPLAINT_RE.search(text)
    # With a verb, catch only "artifact as a detail of another object": if at least
    # a lone artifact word outside the phrase 'a book with a cover' is an order
    # (review 10.11.2026: “make a picture with a cat logo”).
    parts = [m.span("part") for m in _IMAGE_PART_RE.finditer(text)]
    for m in _IMAGE_ARTIFACT_RE.finditer(text):
        if not any(a <= m.start() < b for a, b in parts):
            return True
    return False


def _match_draw_intent(text_lower: str, text_original: str) -> tuple[str, str]:
    """(kind, description): \"draw\" — a request to draw without a trigger at the start of the phrase.
    Empty kind — leave the regular routes (text, edit, repeat) alone."""
    core_start = len(_DRAW_FILLER_RE.match(text_lower).group(0))
    core = text_lower[core_start:]
    # "well, show the picture" means display, not generation: check the lead after the framing.
    # A question ('which logo is better?') and a complaint ('image not loading')
    # are not considered an order (review 10.11.2026).
    if _DRAW_BLOCK_LEAD_RE.search(core) or "?" in core or "？" in core:
        return "", ""
    if _IMAGE_COMPLAINT_RE.search(core):
        return "", ""

    if _match_trigger_prefix(core, DRAW_TRIGGER_PREFIXES):
        # The regular prefix path: it was already handled above in the code; do not duplicate it here.
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

# 'Draw X in the style of this photo': generation with a style reference (as in ChatGPT).
# Triggers only when a photo is attached; otherwise this is ordinary generation.
_STYLE_PHRASES = (
    "в стиле", "по образцу",
    "как на этом фото", "как на этой картинке", "как на этом изображении",
)
_STYLE_RE = re.compile(r"in the style of|like this (?:photo|image)", re.IGNORECASE)


def _wants_style_reference(text: str) -> bool:
    """The user asked to transfer the style from an attached photo, not simply draw something."""
    low = (text or "").lower()
    return any(phrase in low for phrase in _STYLE_PHRASES) or bool(_STYLE_RE.search(low))


# "I want a picture" is not a trigger: it is confused with display/editing. Longer phrases precede shorter ones; otherwise the first match is returned.
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
    # EN: /start promises "read this out loud" (found by an external audit).
    "read this out loud", "read it out loud", "read out loud", "read aloud",
    "voice this", "voice it", "speak this", "say this aloud", "narrate this",
]

def _match_trigger_prefix(text_lower: str, prefixes: list[str]) -> str | None:
    """The longest trigger at the start of a line: \"draw\" came before \"draw, please\" and truncated \"draw, please a cat\" to \"please a cat\" (review 10.11.2026). A word boundary is needed; otherwise \"read\"/\"drawcat\" would produce broken generation."""
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

# Placeholders "this/this text" mean a reply. Found 09.2026: "draw this" drew the word "this".
_REPLY_MARKER_RE = re.compile(
    r"^(это|этот|эта|эту|эти|этого|этому|этим|этих)"
    r"(\s+(текст|сообщение|пост|файл|картинк\w*|изображени\w*|видео|фото))?$",
    re.IGNORECASE,
)

def _strip_reply_marker(content: str) -> str:
    """Removes the demonstrative placeholder from the remainder after the trigger: if nothing else remains, a reply was intended (returns \"\"). Otherwise, the remainder is returned unchanged for literal use. Trailing punctuation (\"this.\", \"this!\") is stripped before the check; otherwise \"read this aloud.\" would only be fixed without the period (found in code review)."""
    if _REPLY_MARKER_RE.match(content.strip().rstrip(".,!?:;—-").strip()):
        return ""
    return content

# A “no file” marker for one model turn, not written to history: otherwise old markers reappear in later turns.
_NO_MEDIA_NOTE = (
    "\n\n[Служебная пометка — это не слова пользователя: к сообщению не "
    "прикреплён файл, и среди недавних файлов чата подходящего нет. Если "
    "в истории выше нет твоего собственного описания именно этого медиа — "
    "честно скажи, что не видишь файла, и попроси прислать его. "
    "Ничего не выдумывай.]"
)

def _history_user_text(user_text: str) -> str:
    """User reply text for history: removes the one-time internal note
    (see _NO_MEDIA_NOTE) if it is at the end. The note has already been sent to
    the current model request; only clean text remains here."""
    if user_text.endswith(_NO_MEDIA_NOTE):
        return user_text[:-len(_NO_MEDIA_NOTE)]
    return user_text


# Chats saved before 30.10.2026 store a one-time service item inside replies: a block
# current date and search results (backup sample: 19 replies out of 210, up to 68% of volume
# user context). A placeholder instead of an answer also remained in the history.
_HISTORY_SERVICE_RE = re.compile(
    r"\s*\[Context: today[^\]]*\]|\s*\[Web search (?:snippets|results)[^\]]{0,200}\][\s\S]*$", re.IGNORECASE)


def _clean_history_entry(item: dict) -> dict | None:
    """History reply without internal blocks; None means discard it entirely."""
    text = item.get("content")
    if not isinstance(text, str):
        return item
    cleaned = _HISTORY_SERVICE_RE.sub("", text).strip()
    if not cleaned or _is_empty_like(cleaned):
        # Media without a caption is a valid message: its [photo] label is built by lumen_memory.
        if item.get("media"):
            return {**item, "content": ""}
        return None
    if cleaned == text:
        return item
    return {**item, "content": cleaned}

# Only explicit media nouns: generic "this/show me" phrases pulled in someone else's photo and caused hallucinations.
# Named groups show WHICH type was asked for (regression 18.08.2026: "show a sticker" described someone else's photo).
_MEDIA_REFERENCE_RE = re.compile(
    r"\b(?:"
    r"(?P<sticker>стикер\w*)|"
    # Negative tails: “graphics card,” “audiobook,” “photosynthesis,”
    # "documentation", "file system" — these are not attachments (audit 29.10.2026).
    r"(?P<video>видео(?!карт|звон|игр|плеер|редактор|наблюден|курс|связ)\w*|видос\w*|ролик\w*|клип\w*|кружок\w*|gif\w*|гиф\w*)|"
    r"(?P<audio>аудио(?!книг|плеер)\w*|голосов\w*|войс\w*)|"
    r"(?P<photo>фото(?!синтез|н\b|ны\b|аппарат|элемент|эффект)\w*|снимок\w*|изображени\w*|скрин\w*|скриншот\w*|картинк\w*)|"
    r"(?P<document>документ(?!аци|овед|альн)\w*|pdf|пдф|файл(?!ов)\w*)"
    r")\b",
    re.IGNORECASE,
)

def _media_reference_category(text: str) -> str | None:
    """Which TYPE of media is mentioned in the text (\"photo\"/\"video\"/\"audio\"/
    \"sticker\"/\"document\"), if any; returns None when there is no explicit
    mention (see _MEDIA_REFERENCE_RE).
    Used by _resolve_incoming_media (priority #3) to search recent_media_ids for
    media of EXACTLY the requested type rather than blindly taking the latest
    file regardless of what was actually asked."""
    if not text:
        return None
    m = _MEDIA_REFERENCE_RE.search(text)
    if not m:
        return None
    return next(name for name, val in m.groupdict().items() if val is not None)

def _looks_like_media_reference(text: str) -> bool:
    """Requires an explicit mention of a specific media type (photo/video/audio/
    sticker, etc.); otherwise see the regression above. Tested separately from
    _handle_message_core."""
    return _media_reference_category(text) is not None

def _mime_matches_media_category(mime: str, category: str) -> bool:
    """The MIME type fits the category: stickers are image/webp only, photos are converted to JPEG, which is enough without a Sticker object."""
    low = (mime or "").lower()
    if category == "sticker":
        return low == "image/webp"
    if category == "photo":
        return low.startswith("image/") and low != "image/webp"
    if category in ("video", "audio"):
        return low.startswith(f"{category}/")
    if category == "document":
        # PDF/text/office files + octet-stream (an unrecognized binary —
        # an honest error will still reach the user later from ask_gemini via
        # _is_gemini_supported_mime, rather than a silent "file not found").
        return low.startswith(("application/", "text/"))
    return False

# Clarification buttons (pick scenarios)
# Pick rather than letting the model guess: taste verb + subject + short; long requests with details go to the model.
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
# Game forms — only with boundaries on both sides: the "igr" prefix (transliterated) caught verbs
# ("who is playing today" is not a game request). Keep infinitives: "what to play" is about games.
_PICK_EXACT_TOPICS: dict[str, str] = {
    "игра": "games", "игры": "games", "игру": "games", "игрой": "games",
    "игре": "games", "игр": "games", "играть": "games", "поиграть": "games",
}
_PICK_MAX_LEN = 60

def match_pick_request(text_lower: str) -> str | None:
    """Returns the pick-scenario id (film/music/...) if the text is a preference
    request without details, otherwise None. Checked in _handle_message_core before
    routing to AI: matching requests show buttons instead of making the model guess."""
    text = text_lower.strip()
    if not text or len(text) > _PICK_MAX_LEN:
        return None
    # Details (year, comma-separated lists) go through the normal path to the
    # model:
    # such a request already has a specific style; no buttons are needed.
    if re.search(r"\b(19|20)\d{2}\b", text) or text.count(",") >= 2:
        return None
    if not any(v in text for v in _PICK_VERBS):
        return None
    for topic, scenario in _PICK_EXACT_TOPICS.items():
        if re.search(r"(?<!\w)" + re.escape(topic) + r"(?!\w)", text):
            return scenario
    for topic, scenario in _PICK_TOPICS.items():
        # Latin strictly uses ; Cyrillic uses a prefix (AUD-E-006): "tiger" must not match "games".
        if topic.isascii():
            if re.search(r"\b" + re.escape(topic) + r"\b", text):
                return scenario
        elif re.search(r"(?<!\w)" + re.escape(topic), text):
            return scenario
    return None


def _find_recent_media_by_category(bucket: Any, category: str) -> tuple[str, str] | None:
    """The last element of the bucket of the MATCHING type (not merely the last one): \"show a sticker\" with [photo] returns None."""
    if not bucket:
        return None
    for fid, mime in reversed(bucket):
        if _mime_matches_media_category(mime, category):
            return fid, mime
    return None
