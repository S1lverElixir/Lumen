"""
lumen_formatting.py — конвертация markdown-подобного текста Lumen в Telegram HTML.

Вынесено из bot.py при аудите технического долга (см. пункт про монолитный
bot.py, который на момент этого разбиения на модули разросся до нескольких
тысяч строк): вся эта логика — чистые функции над строками (никакой
Telegram/Gemini/OpenRouter I/O, никакого рантайм-состояния) и поэтому один из
самых безопасных кандидатов на выделение в отдельный модуль. bot.py импортирует
из этого файла все нужные имена напрямую (см. `from lumen_formatting import ...`
в bot.py) — поведение и публичные имена (`_md_to_html`, `_scrub_latex` и т.д.)
не изменились, изменилось только физическое расположение кода.
"""

from __future__ import annotations

import re

_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

def _split_table_cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]

def _convert_markdown_tables_to_lists(text: str) -> str:
    """Telegram не рендерит markdown-таблицы НИ В КАКОМ режиме (ни HTML, ни
    MarkdownV2) — реальный найденный при тестировании случай: модель (особенно
    некоторые модели OpenRouter) игнорирует запрет на таблицы из system_prompt.py
    и всё равно генерирует '|---|---|', пользователь видит вместо аккуратной
    таблицы сырую кашу из символов "|" построчно. Это защитный (второй) рубеж —
    находит блоки вида "заголовок + строка-разделитель из дефисов + строки
    данных" и разворачивает их в список пунктов "**Заголовок:** значение",
    группируя ячейки одной строки в один пункт списка."""
    if "|" not in text or "-" not in text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if "|" in line and i + 1 < n and "-" in lines[i + 1] and _TABLE_SEP_RE.match(lines[i + 1]):
            header_cells = _split_table_cells(line)
            if len(header_cells) >= 2:
                data_rows = []
                j = i + 2
                while j < n and "|" in lines[j] and lines[j].strip():
                    data_rows.append(_split_table_cells(lines[j]))
                    j += 1
                if data_rows:
                    for row in data_rows:
                        parts = []
                        for h_idx, header in enumerate(header_cells):
                            val = row[h_idx] if h_idx < len(row) else ""
                            if not val:
                                continue
                            parts.append(f"**{header}:** {val}" if header else val)
                        if parts:
                            out.append("• " + "; ".join(parts))
                    i = j
                    continue
        out.append(line)
        i += 1
    return "\n".join(out)

# ── Защитная сетка от сырого LaTeX ──────────────────────────────────────────
# Реальный найденный при калибровке случай: nemotron-3-nano-30b-a3b:free выдала
# "\[ S = \pi r^{2}, \]" и "\(x^{2}+y^{2}=r^{2}\)" вместо юникода в ответе про
# площадь круга — при том что system_prompt.py прямо запрещает LaTeX и явно
# перечисляет юникод-замены (см. раздел ФОРМАТИРОВАНИЕ). Инструкция в промпте —
# первый (и ненадёжный) рубеж; это — второй, тот же принцип, что уже применяется
# к случайным HTML-тегам в Phase 0 ниже: не полагаемся только на послушание
# модели, страхуем детерминированной пост-обработкой.
_LATEX_SUPERSCRIPT_MAP = {"0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "n": "ⁿ"}
_LATEX_SUBSCRIPT_MAP = {"0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉"}
# Порядок важен: многобуквенные команды (\times, \infty...) должны замениться
# ДО одиночного \t/\i и т.п., иначе оставшийся общий "\команда -> без бэкслеша"
# в конце срежет их раньше времени. dict сохраняет порядок вставки в Python 3.7+.
_LATEX_SYMBOL_MAP: dict[str, str] = {
    r"\times": "×", r"\cdot": "·", r"\approx": "≈", r"\infty": "∞",
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥", r"\neq": "≠", r"\ne": "≠",
    r"\rightarrow": "→", r"\Rightarrow": "⇒", r"\to": "→",
    r"\forall": "∀", r"\exists": "∃", r"\emptyset": "∅", r"\cup": "∪", r"\cap": "∩", r"\in": "∈",
    r"\pi": "π", r"\pm": "±", r"\mp": "∓", r"\sum": "∑", r"\int": "∫", r"\prod": "∏",
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\Gamma": "Γ", r"\theta": "θ",
    r"\lambda": "λ", r"\mu": "μ", r"\sigma": "σ", r"\Sigma": "Σ", r"\delta": "δ", r"\Delta": "Δ",
    r"\phi": "φ", r"\omega": "ω", r"\Omega": "Ω",
}

def _latex_superscript(m: re.Match) -> str:
    return "".join(_LATEX_SUPERSCRIPT_MAP.get(ch, ch) for ch in m.group(1))

def _latex_subscript(m: re.Match) -> str:
    return "".join(_LATEX_SUBSCRIPT_MAP.get(ch, ch) for ch in m.group(1))

def _scrub_latex(text: str) -> str:
    """Конвертирует сырой LaTeX в обычный юникод-текст (или снимает разметку,
    если точный эквивалент неизвестен) — ДОЛЖНА вызываться уже после того, как
    настоящие блоки/спаны кода вырезаны и заменены плейсхолдерами (см. Phase 1 в
    _md_to_html), иначе легитимный код с обратным слэшем (regex-паттерны, пути
    Windows и т.п.) был бы испорчен."""
    if "\\" not in text and "$" not in text:
        return text
    # Разделители-обёртки $$...$$, \[...\], \(...\) — убираем сами разделители,
    # оставляя содержимое для дальнейшей посимвольной замены ниже. Одиночный
    # "$...$" (инлайн-математика в LaTeX) НАМЕРЕННО не обрабатывается: найдено
    # при код-ревью — если в одном сообщении встречаются и сумма в долларах, и
    # настоящая формула ("цена $100, а формула $x^2$ рядом"), первый "$" суммы
    # ошибочно спаривается с первым "$" формулы, и результат становится ХУЖЕ
    # исходного (обрезанные суммы плюс осиротевший "$" в хвосте формулы — то есть
    # именно тот класс "лишнего символа", который эта защитная сетка должна
    # убирать, а не плодить). "$$...$$" безопаснее: два подряд идущих "$" без
    # пробела между ними практически никогда не возникают в обычном тексте с
    # суммами денег, поэтому ложные срабатывания здесь на практике не встречаются.
    text = re.sub(r"\\\[(.*?)\\\]", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\\\((.*?)\\\)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\$\$(.*?)\$\$", r"\1", text, flags=re.DOTALL)
    # \frac{a}{b} -> a/b (одноуровневая вложенность, самый частый случай)
    text = re.sub(r"\\d?frac\{([^{}]*)\}\{([^{}]*)\}", r"\1/\2", text)
    # \sqrt{x} -> √x, \sqrt[n]{x} -> ⁿ√x
    text = re.sub(r"\\sqrt\[([^\]]*)\]\{([^{}]*)\}", r"\1√\2", text)
    text = re.sub(r"\\sqrt\{([^{}]*)\}", r"√\1", text)
    for cmd, repl in _LATEX_SYMBOL_MAP.items():
        text = text.replace(cmd, repl)
    # x^{2} / x^2 -> x², x_{2} / x_2 -> x₂ — только короткие индексы/степени,
    # чтобы случайно не тронуть код вида a^b в языках, где это не степень.
    # ОСТАТОЧНЫЙ EDGE-CASE (осознанно принят, не фиксим): замена не привязана к
    # "$"/"\("-разделителям и срабатывает на голое "x^2" где угодно в тексте вне
    # код-блоков/код-спанов (те уже вырезаны на предыдущем шаге). Если модель
    # без backtick-форматирования упомянет побитовый XOR в прозе ("5^3 даёт..."),
    # это тоже превратится в "5³" — потеряв смысл XOR. Системный промпт и так
    # требует оформлять код через `бэктики`/```блоки```, поэтому легитимные
    # примеры кода уже защищены; голый "^" в чистой прозе почти всегда всё же
    # означает именно степень, а не XOR — компромисс в пользу частого случая.
    text = re.sub(r"\^\{([0-9n+\-]{1,3})\}", _latex_superscript, text)
    text = re.sub(r"\^([0-9n])(?![0-9])", _latex_superscript, text)
    text = re.sub(r"_\{([0-9]{1,3})\}", _latex_subscript, text)
    text = re.sub(r"_([0-9])(?![0-9])", _latex_subscript, text)
    # Оставшиеся одиночные \command без известного юникод-эквивалента — просто
    # снимаем бэкслеш, чтобы пользователь не видел сырое "\int"/"\mathbb" и т.п.
    text = re.sub(r"\\([a-zA-Z]+)", r"\1", text)
    return text

# ── Маркеры списков "- текст" / "* текст" в начале строки → "• текст" ───────
# Реальный найденный при калибровке пробел: _md_to_html конвертирует **bold**,
# *italic*, `code`, ```блоки```, markdown-таблицы — но НЕ конвертирует обычные
# markdown-маркеры списков, которые system_prompt.py явно предписывает
# использовать вместо таблиц ("маркированный список"). Модель пишет "- Пункт"
# или "* Пункт" (оба — совершенно нормальный markdown), а пользователь в
# Telegram видел литеральные "-"/"*" в начале строки вместо аккуратного "•".
# Заменяем маркер целиком (а не оставляем "*" как есть) — так исключается и
# побочный риск, что одиночная "*" в начале строки случайно спарится с другой
# "*" где-то дальше в тексте и даст неверный *italic*.
_BULLET_MARKER_RE = re.compile(r"^([ \t]*)[-*][ \t]+", re.MULTILINE)

def _normalize_bullet_markers(text: str) -> str:
    return _BULLET_MARKER_RE.sub(lambda m: m.group(1) + "• ", text)

# ── Markdown-заголовки "#"/"##"/"###" → **жирный текст** ────────────────────
# РЕГРЕССИЯ, найденная при живом тестировании (18 августа 2026, реальные скриншоты
# из Telegram): system_prompt.py прямо запрещает markdown-заголовки ("Также никогда
# не используй markdown-заголовки (#, ##, ### и т.п.)... Для выделения структуры
# используй жирный текст (**...**) вместо заголовков"), но модели (особенно
# бесплатные модели OpenRouter) регулярно их всё равно пишут — то же самое
# несоблюдение инструкции моделью, которое уже потребовало защитных сеток для
# таблиц (_convert_markdown_tables_to_lists) и маркеров списков
# (_normalize_bullet_markers) выше. Без скрубера здесь "### Как она выводится?"
# уходит в Telegram буквально с "###" в начале строки — реальный найденный баг,
# видимый на скриншотах. ATX-заголовок распознаётся по стандартному правилу
# CommonMark: 1-6 "#" в начале строки, затем обязательно пробел/таб — это
# исключает "C#"/"#tag" и подобные легитимные "#" не в начале строки (те тут
# не попадают под шаблон вообще, т.к. якорь "^" требует начала строки).
_HEADER_MARKER_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+(.*)$", re.MULTILINE)

def _normalize_headers(text: str) -> str:
    def _repl(m: re.Match) -> str:
        content = m.group(1).rstrip()
        if not content:
            # Голая строка из одних "#" без текста — нечего выделять жирным,
            # просто убираем маркер целиком, а не оставляем пустую "****".
            return ""
        if content.startswith("**") and content.endswith("**") and len(content) > 4:
            # Модель сама уже обернула текст заголовка в **bold** (нередкий
            # случай — "### **Важно**") — оборачивать ЕЩЁ раз дало бы "****Важно****"
            # и сломало бы парность звёздочек в Phase 3 ниже. Раз обёртка уже
            # есть, только снимаем сам маркер "#", остальное не трогаем.
            return content
        return f"**{content}**"
    return _HEADER_MARKER_RE.sub(_repl, text)

# ── Markdown-цитаты "> текст" → Telegram <blockquote> ───────────────────────
# Тот же принцип, что и у _normalize_bullet_markers выше: "> " — обычный
# GFM-синтаксис цитаты, модели он известен без единого слова в system_prompt.py
# (там про цитаты вообще ничего не сказано — как и про списки, см. комментарий
# у _normalize_bullet_markers). Раньше строка "> текст" просто уходила в
# Telegram буквально с ">" в начале. Строится ДО HTML-экранирования (Phase 2),
# как и остальные построчные нормализации этой секции — сама обёртка
# <blockquote> добавляется тут же, а не как markdown-маркер для Phase 3, чтобы
# не путать её с обычным ">" внутри текста (например, "5 > 3").
_BLOCKQUOTE_LINE_RE = re.compile(r"^> ?(.*)$")
# \x00-сентинелы вместо буквальных <blockquote>/</blockquote> — та же причина,
# что и у плейсхолдеров код-блоков в _md_to_html (см. Phase 1 там): если вставить
# реальный HTML-тег здесь, Phase 2 (HTML-escape) его же и экранирует. Сентинелы
# невидимы для escape (тот трогает только &/</>) и заменяются на настоящие теги
# уже ПОСЛЕ Phase 3 — так текст внутри цитаты всё ещё проходит обычные
# escape/markdown-фазы (например, "> **важно**" корректно станет цитатой с
# жирным текстом внутри), меняется только сама обёртка.
_BLOCKQUOTE_START = "\x00BQS\x00"
_BLOCKQUOTE_END = "\x00BQE\x00"

def _convert_blockquotes(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    quote_buf: list[str] = []

    def _flush():
        if quote_buf:
            out.append(_BLOCKQUOTE_START + "\n".join(quote_buf) + _BLOCKQUOTE_END)
            quote_buf.clear()

    for line in lines:
        m = _BLOCKQUOTE_LINE_RE.match(line)
        if m:
            quote_buf.append(m.group(1))
        else:
            _flush()
            out.append(line)
    _flush()
    return "\n".join(out)

def _md_to_html(text: str) -> str:
    """Convert markdown-like text to Telegram HTML.

    ── КОНТРАКТ ПАЙПЛАЙНА (аудит техдолга, см. пункт про фрагильность этой функции) ──
    Это цепочка НЕЗАВИСИМЫХ regex-проходов поверх одного текста, а не нормальный
    парсер с единым деревом разбора — каждый следующий шаг видит результат
    предыдущего, и порядок шагов принципиален. Сознательное решение НЕ переписывать
    это на полноценный токенизатор прямо сейчас: пайплайн уже покрыт ~20 тестами,
    которые ловят именно межфазовые конфликты (см. test_scrub_latex_order_sensitive_
    replacements_dont_corrupt_each_other, test_md_to_html_does_not_touch_pipes_inside_
    code_block, test_scrub_latex_does_not_confuse_currency_with_math_delimiters и
    т.п.) — переписывание на парсер потребовало бы повторно доказать корректность
    каждого из этих уже отлаженных на реальных инцидентах edge-case'ов заново, без
    реального выигрыша в надёжности, который можно было бы проверить иначе, чем тем
    же самым живым продакшен-трафиком, что уже нашёл текущие edge-case'ы. Если в
    будущем добавится ещё один вид форматирования и очередной межфазовый конфликт
    станет реальной проблемой (а не гипотетической) — тогда и стоит пересматривать
    архитектуру, а не превентивно.

    Обязательный порядок фаз (нарушение порядка ломает уже отлаженные edge-case'ы):
      0. Нормализация сырых HTML-тегов (<b>/<i>/<code>/<pre> и битые self-closing) в
         markdown-эквивалент — ДО экранирования (шаг 2), иначе легитимные теги от
         модели превратились бы в видимый мусор "&lt;b&gt;".
      1. Код-блоки/спаны (```...```/`...`) вырезаются и заменяются плейсхолдерами —
         ДО LaTeX/таблиц/списков/markdown, иначе обратные слэши и "|"/"-" внутри
         реального кода (regex, пути Windows, побитовое ИЛИ) были бы испорчены.
      1.3. LaTeX → юникод (_scrub_latex) — код уже вынесен шагом 1.
      1.4. Маркеры списков "-"/"* " → "•" (_normalize_bullet_markers) — ДО таблиц,
           чтобы строка-разделитель таблицы ("|---|---|") успела обработаться первой
           и не была принята за маркер списка.
      1.41. Markdown-заголовки "#"/"##"/"###" → **жирный текст** (_normalize_headers) —
            после списков (не пересекаются по синтаксису), до Phase 3, чтобы
            получившиеся "**...**" были обработаны обычным bold-регэкспом ниже
            на общих основаниях, а не отдельной веткой.
      1.45. Markdown-цитаты "> " → сентинелы \x00BQS\x00/\x00BQE\x00 (_convert_
            blockquotes) — сентинелы, не сразу <blockquote>, т.к. Phase 2 экранировал
            бы буквальный тег; настоящий тег подставляется после Phase 3 (см. ниже).
      1.5. Markdown-таблицы → список пунктов (_convert_markdown_tables_to_lists) —
           код и списки уже обработаны/вырезаны шагами 1/1.4.
      2. HTML-экранирование остального текста (&/</>).
      3. Markdown (**bold**/*italic*/~~strike~~/[текст](url)) → HTML-теги — ПОСЛЕ
         экранирования, иначе символы разметки сами могли бы быть экранированы
         раньше времени. Ссылки [текст](url) — последними в этой фазе (после bold/
         italic), чтобы regex-проходы italic/bold не залезли внутрь href, если URL
         содержит "_" (см. комментарий в коде).
      3.5. Сентинелы цитаты (шаг 1.45) → настоящий <blockquote> — после Phase 3,
           чтобы markdown внутри цитаты успел стать HTML до финализации обёртки.
      4. Код-блоки/спаны восстанавливаются из плейсхолдеров с собственным
         экранированием — самыми последними, чтобы шаги 2-3 их не затронули.

    Code blocks are saved first so underscores/asterisks inside them
    are never treated as italic/bold markers.
    """
    if not text:
        return ""

    # ── Phase 0: нормализация "сырых" HTML-тегов, которые модель иногда пишет
    # напрямую вместо markdown (несмотря на явную инструкцию в system_prompt.py
    # использовать только markdown-синтаксис) — без этого такие теги ловятся
    # escape'ом на шаге 2 и показываются пользователю как видимый мусорный текст
    # вида "<b>"/"<b/>" прямо в сообщении (реальный найденный при тестировании
    # баг). Сначала убираем заведомо битые self-closing варианты (напр. "<b/>"),
    # затем конвертируем корректные парные теги в markdown-эквивалент — дальше
    # они идут по тому же (уже проверенному) конвейеру, что и обычный markdown.
    text = re.sub(r"</?(?:b|strong|i|em|u|s|code|pre)\s*/>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<(?:b|strong)>(.*?)</(?:b|strong)>", r"**\1**", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<(?:i|em)>(.*?)</(?:i|em)>", r"*\1*", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<u>(.*?)</u>", r"\1", text, flags=re.IGNORECASE | re.DOTALL)
    # spoiler — тот же трюк, что и <u> выше: system_prompt.py не просит модель их
    # использовать, поэтому это чисто защитная сетка на случай, если модель всё же
    # напишет буквальный тег. Раньше он не ловился здесь вообще и долетал до Phase 2
    # экранирования — показывался пользователю как видимый мусор "&lt;tg-spoiler&gt;".
    text = re.sub(r"<tg-spoiler>(.*?)</tg-spoiler>", r"\1", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<span\s+class=["\']tg-spoiler["\']>(.*?)</span>', r"\1", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<s>(.*?)</s>", r"~~\1~~", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<pre>(.*?)</pre>", lambda m: f"```\n{m.group(1)}\n```", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.IGNORECASE | re.DOTALL)

    # ── Phase 1: Save code spans/blocks before any processing ────────────────
    _saved: dict[str, str] = {}
    _counter = [0]

    def _save_block(m: re.Match) -> str:
        key = f"\x00CB{_counter[0]}\x00"
        _counter[0] += 1
        _saved[key] = m.group(0)
        return key

    text = re.sub(r"```[a-zA-Z0-9]*\n.*?\n```", _save_block, text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]+`", _save_block, text)
    # [текст](url) — вырезается ТЕМ ЖЕ плейсхолдером, что и код, а не обрабатывается
    # позже в Phase 3: реальный найденный при написании этого фикса баг — url внутри
    # скобок часто содержит "_" (например "foo_bar" в пути), и независимо от того,
    # раньше или позже bold/italic-регэкспов Phase 3 конвертировать ссылку, пара
    # таких "_" в сыром "[текст](url)" либо корёжит сам italic-регэксп (если ссылка
    # конвертируется позже), либо потенциально ловится regex-проходами уже после
    # вставки <a href="..."> (если раньше). Полностью выведена из-под удара — текст
    # ссылки и url не проходят через LaTeX/bold/italic вообще, восстанавливаются как
    # есть в Phase 4 (без поддержки markdown внутри текста ссылки — не запрашивалось).
    text = re.sub(r"\[([^\[\]]+)\]\((https?://[^\s()]+)\)", _save_block, text)

    # ── Phase 1.3: сырой LaTeX → юникод (см. _scrub_latex выше) — код уже
    # вынесен на предыдущем шаге, поэтому обратные слэши в реальном коде
    # (regex, пути Windows и т.п.) не затрагиваются.
    text = _scrub_latex(text)

    # ── Phase 1.4: маркеры списков "- "/"* " → "• " (см. _normalize_bullet_
    # markers выше) — ДО таблиц и ДО Phase 3, чтобы не путаться с "**bold**" и
    # чтобы строка-разделитель таблицы ("|---|---|") успела обработаться первой.
    text = _normalize_bullet_markers(text)

    # ── Phase 1.41: markdown-заголовки "#"/"##"/"###" → **жирный текст** (см.
    # _normalize_headers выше) — после списков, до HTML-экранирования и до Phase 3
    # (получившийся "**...**" обрабатывается обычным bold-регэкспом на общих
    # основаниях, отдельная ветка не нужна).
    text = _normalize_headers(text)

    # ── Phase 1.45: markdown-цитаты "> " → сентинелы blockquote (см.
    # _convert_blockquotes выше) — ДО HTML-экранирования, т.к. решение "это
    # строка цитаты" принимается по буквальному "> " в начале строки; сама
    # обёртка <blockquote> подставляется позже (после Phase 3), сентинелы же
    # (\x00BQS\x00/\x00BQE\x00) escape в Phase 2 не трогает.
    text = _convert_blockquotes(text)

    # ── Phase 1.5: markdown-таблицы → список пунктов (см. _convert_markdown_
    # tables_to_lists выше) — код уже вынесен на предыдущем шаге, поэтому "|"
    # внутри кода (например, битовое ИЛИ в Rust/C) сюда не попадёт.
    text = _convert_markdown_tables_to_lists(text)

    # ── Phase 2: HTML-escape the rest ────────────────────────────────────────
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # ── Phase 3: Apply markdown ───────────────────────────────────────────────
    text = re.sub(r"(\*\*|__)(.*?)\1", r"<b>\2</b>", text, flags=re.DOTALL)
    text = re.sub(r"(\*|_)(.*?)\1", r"<i>\2</i>", text)
    text = re.sub(r"~~(.*?)~~", r"<s>\1</s>", text)

    # ── Phase 3.5: blockquote-сентинелы → настоящие <blockquote> ─────────────
    # После Phase 3, чтобы markdown внутри цитаты (например "> **важно**")
    # успел превратиться в HTML до того, как обёртка станет реальным тегом.
    text = text.replace(_BLOCKQUOTE_START, "<blockquote>").replace(_BLOCKQUOTE_END, "</blockquote>")

    # ── Phase 4: Restore code blocks with proper escaping ────────────────────
    for key, orig in _saved.items():
        if orig.startswith("```"):
            m = re.match(r"```([a-zA-Z0-9]*)\n(.*)\n```", orig, re.DOTALL)
            lang, inner = (m.group(1), m.group(2)) if m else ("", orig[3:-3])
            inner = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            # language — атрибут entity "pre" в Telegram (даёт подсветку синтаксиса
            # в клиентах, которые её поддерживают); раньше язык из ```python вырезался
            # регэкспом при сохранении, но никогда не доходил до вывода.
            replacement = f'<pre><code class="language-{lang}">{inner}</code></pre>' if lang else f"<pre>{inner}</pre>"
        elif orig.startswith("["):
            # markdown-ссылка [текст](url) — см. Phase 1 выше про то, почему вырезана
            # плейсхолдером, а не обработана в Phase 3. Текст ссылки восстанавливается
            # как обычный экранированный текст (markdown внутри него — **/*/` и т.п. —
            # намеренно НЕ поддерживается, это не запрашивалось; при необходимости
            # добавить — рекурсивный вызов _md_to_html на group(1) прямо здесь).
            m = re.match(r"\[([^\[\]]+)\]\((https?://[^\s()]+)\)", orig, re.DOTALL)
            label, url = m.group(1), m.group(2)
            label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            url = url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            replacement = f'<a href="{url}">{label}</a>'
        else:
            inner = orig[1:-1]
            inner = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            replacement = f"<code>{inner}</code>"
        text = text.replace(key, replacement)

    return text

