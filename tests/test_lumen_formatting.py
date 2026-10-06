"""
test_lumen_formatting.py — юнит-тесты на lumen_formatting.py: конвертация markdown-подобного
текста Lumen в Telegram HTML (_md_to_html), защитная сетка от сырого LaTeX (_scrub_latex),
нормализация маркеров списков (_normalize_bullet_markers).

Часть разбиения test_bot_helpers.py по модулям вслед за уже существующим разбиением
исходников (lumen_formatting.py / lumen_security.py / lumen_router_config.py / bot.py) —
см. README, аудит техдолга. lumen_formatting.py не имеет ни одной зависимости от
Telegram/Gemini/OpenRouter/рантайм-состояния бота, поэтому этот файл тестирует его
напрямую (import lumen_formatting), без импорта bot.py — не привязан к переменным
окружения BOT_TOKEN/GEMINI_API_KEY и т.п., которые conftest.py подставляет только ради
самого bot.py.

Запуск:
    pytest test_lumen_formatting.py -v
"""
import lumen_formatting
import pytest
import time



# ─────────────────────────── _truncate_html_to_fit ───────────────────────────

def test_truncate_html_to_fit_keeps_short_text_and_cuts_at_source_boundary():
    # Регрессия AUD-J-002: рез готового HTML рвал теги ("can't parse entities").
    # Переехала из test_bot.py вслед за функцией (TG_MAX_LEN=4096 — лимит Telegram).
    short = lumen_formatting._truncate_html_to_fit("привет", 4096)
    assert short == lumen_formatting._md_to_html("привет")
    long_md = "**" + "x" * 5000 + "**"
    cut = lumen_formatting._truncate_html_to_fit(long_md, 4096)
    assert len(cut) <= 4096
    assert cut.endswith("…")
    assert cut.count("<b>") == cut.count("</b>")


# ─────────────────────────── _md_to_html ───────────────────────────

@pytest.mark.parametrize(("source", "expected"), [
    ("", ""),
    ("**bold**", "<b>bold</b>"),
    ("*italic*", "<i>italic</i>"),
    ("~~strike~~", "<s>strike</s>"),
    ("`code`", "<code>code</code>"),
])
def test_md_to_html_basic_markup(source, expected):
    assert lumen_formatting._md_to_html(source) == expected


def test_md_to_html_escapes_raw_html():
    # Сырой HTML от модели не должен пролезть как есть — иначе Telegram
    # либо сломает parse_mode=HTML, либо (хуже) отрендерит чужую разметку.
    assert lumen_formatting._md_to_html("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_md_to_html_inline_code():
    assert lumen_formatting._md_to_html("`code`") == "<code>code</code>"


def test_md_to_html_code_block():
    # Язык фенса сохраняется как class="language-x" для подсветки в Telegram.
    assert lumen_formatting._md_to_html("```python\nprint(1)\n```") == '<pre><code class="language-python">print(1)</code></pre>'


def test_md_to_html_escapes_inside_code_block():
    # Код-теги внутри code/pre тоже обязаны экранироваться, иначе строка вида
    # "`<tag>`" сломает HTML-разметку сообщения в Telegram.
    assert lumen_formatting._md_to_html("`<tag>`") == "<code>&lt;tag&gt;</code>"


def test_md_to_html_no_html_injection_via_markdown_markers():
    # Регрессионный тест на реальный инцидент: markdown-символы внутри текста
    # не должны давать невалидную HTML-разметку (непарные теги).
    result = lumen_formatting._md_to_html("**bold** and *italic* and `code`")
    assert result.count("<b>") == result.count("</b>")
    assert result.count("<i>") == result.count("</i>")
    assert result.count("<code>") == result.count("</code>")


def test_table_separator_match_is_time_bounded():
    # Квадратичный регекс на пробельных хвостах зависал: строка режется до match.
    line = "| a | b |\n" + " " * 12000 + "-"
    started = time.monotonic()
    lumen_formatting._convert_markdown_tables_to_lists(line)
    assert time.monotonic() - started < 1.0
    # Обычный разделитель по-прежнему детектится.
    assert lumen_formatting._is_table_separator("|---|---|") is True
    assert lumen_formatting._is_table_separator(" --- | --- ") is True
    assert lumen_formatting._is_table_separator("просто текст") is False


def test_md_to_html_normalizes_raw_html_bold_tag():
    # Регрессия: модель иногда пишет литеральные <b>/<i> теги вместо markdown
    # (несмотря на инструкцию в system_prompt.py) — раньше это экранировалось
    # и показывалось пользователю как видимый мусорный текст вида "<b>".
    assert lumen_formatting._md_to_html("<b>Заголовок</b>") == "<b>Заголовок</b>"


def test_md_to_html_strips_broken_self_closing_tag():
    # Реальный найденный баг: модель иногда пишет невалидный self-closing "<b/>".
    result = lumen_formatting._md_to_html("текст <b/> ещё текст")
    assert "<b/>" not in result
    assert "&lt;b/&gt;" not in result


def test_md_to_html_normalizes_raw_html_code_and_pre():
    assert lumen_formatting._md_to_html("код: <code>print(1)</code>") == "код: <code>print(1)</code>"
    result = lumen_formatting._md_to_html("<pre>def f():\n    pass</pre>")
    assert result.startswith("<pre>") and result.endswith("</pre>")


def test_md_to_html_still_escapes_ordinary_comparison_operators():
    # Убеждаемся, что нормализация тегов не сломала обычное экранирование —
    # "5 < 10" не должно превращаться в незакрытый тег.
    assert lumen_formatting._md_to_html("сравнение: 5 < 10") == "сравнение: 5 &lt; 10"


@pytest.mark.parametrize(("text", "present"), [
    # Регрессия: Telegram не рендерит markdown-таблицы ни в каком режиме —
    # пользователь видел сырой текст с "|" и "---" (подтверждено скриншотами).
    (
        "| Аспект | React | Vue |\n"
        "|--------|-------|-----|\n"
        "| Кривая обучения | Высокая | Низкая |\n"
        "| Сообщество | Огромное | Среднее |",
        ("<b>Аспект:</b> Кривая обучения", "<b>React:</b> Высокая", "<b>Vue:</b> Низкая"),
    ),
    # Некоторые модели пишут таблицы без внешних "|" по краям строки.
    (
        "Название | Цена\n"
        "---|---\n"
        "Кофе | 150\n"
        "Чай | 100",
        ("<b>Название:</b> Кофе", "<b>Цена:</b> 150"),
    ),
])
def test_md_to_html_converts_markdown_tables_to_bullet_lists(text, present):
    result = lumen_formatting._md_to_html(text)
    assert "|" not in result
    assert all(p in result for p in present)
    assert result.count("•") == 2


def test_md_to_html_does_not_touch_pipes_inside_code_block():
    # "|" внутри блока кода (например, побитовое ИЛИ в Rust/C) не должно
    # ошибочно распознаваться как таблица — код уже вынесен на предыдущем шаге.
    text = "```rust\nlet x = a | b;\nlet y = c | d;\n```"
    result = lumen_formatting._md_to_html(text)
    assert "let x = a | b;" in result
    assert "•" not in result


def test_md_to_html_no_false_positive_on_plain_text_with_dashes():
    # Обычный текст с дефисами (не таблица) не должен ломаться конвертером.
    text = "Список дел:\n- сходить в магазин\n- купить хлеб"
    result = lumen_formatting._md_to_html(text)
    assert "сходить в магазин" in result
    assert "купить хлеб" in result


# ─────────────────── LaTeX-скрубер (защитная сетка от сырого LaTeX) ───────────────────
# Регрессия на реальный найденный при калибровке случай: nemotron-3-nano-30b-a3b:free
# выдала "\[ S = \pi r^{2}, \]" и "\(x^{2}+y^{2}=r^{2}\)" вместо юникода, несмотря на
# явный запрет LaTeX в system_prompt.py.

@pytest.mark.parametrize(("source", "gone", "present"), [
    (r"Площадь: \[ S = \pi r^{2} \]", ("\\[", "\\]"), ("π", "r²")),
    (r"формула \(x^{2}+y^{2}=r^{2}\)", ("\\(", "\\)"), ("x²+y²=r²",)),
])
def test_scrub_latex_converts_delimited_math(source, gone, present):
    result = lumen_formatting._scrub_latex(source)
    assert all(g not in result for g in gone)
    assert all(p in result for p in present)


@pytest.mark.parametrize(("source", "expected"), [
    (r"\frac{1}{2}", "1/2"),
    (r"\sqrt{16}", "√16"),
    ("обычный текст без формул", "обычный текст без формул"),
    ("$$x^2 + y^2$$", "x² + y²"),
])
def test_scrub_latex_exact_replacements(source, expected):
    assert lumen_formatting._scrub_latex(source) == expected


def test_scrub_latex_converts_common_symbols():
    result = lumen_formatting._scrub_latex(r"\times \pm \leq \geq \infty \sum \int")
    for leftover in ("\\times", "\\pm", "\\leq", "\\geq", "\\infty", "\\sum", "\\int"):
        assert leftover not in result
    assert "×" in result and "±" in result and "≤" in result and "≥" in result and "∞" in result


def test_scrub_latex_does_not_confuse_currency_with_math_delimiters():
    # РЕГРЕССИЯ, найденная при code-review (25 июля 2026): первая версия скрубера
    # обрабатывала и одиночный "$...$" как инлайн-LaTeX. Если в одном сообщении
    # встречались и сумма в долларах, и настоящая формула ("цена $100, а формула
    # $x^2$ рядом"), первый "$" суммы ошибочно спаривался с первым "$" формулы —
    # результат был ХУЖЕ исходного: обрезанные суммы плюс осиротевший "$" в хвосте
    # ("цена 100, а формула x²$ рядом"). Одиночный "$" теперь не обрабатывается
    # вообще — только "$$...$$". Сами доллары остаются нетронутыми в обоих случаях;
    # "x^2" внутри всё равно аккуратно превращается в "x²" — это отдельная, не
    # завязанная на "$"-разделители замена (см. test_scrub_latex_exact_replacements),
    # она безвредна и здесь, и вне контекста "$".
    assert lumen_formatting._scrub_latex("цена $100, а формула $x^2$ рядом") == "цена $100, а формула $x²$ рядом"
    assert lumen_formatting._scrub_latex("первый вариант — $50, второй — $100") == "первый вариант — $50, второй — $100"
    assert lumen_formatting._scrub_latex("стоимость: $100. Итого: $200.") == "стоимость: $100. Итого: $200."


def test_scrub_latex_order_sensitive_replacements_dont_corrupt_each_other():
    # РЕГРЕССИЯ НА БУДУЩЕЕ: _LATEX_SYMBOL_MAP — это plain str.replace() в порядке
    # вставки словаря, а не regex. "\le" — подстрока "\leq", "\in" — подстрока
    # "\infty" ("\in" + "fty"). Если порядок в словаре когда-нибудь поменяют так,
    # что короткая команда окажется раньше длинной, начинающейся с той же
    # подстроки, результат будет испорчен ("∈fty" вместо "∞" и т.п.). Здесь фикс
    # ИМЕННО порядка (leq/geq/neq/infty перед le/ge/ne/in) — тест проверяет
    # итоговое поведение, а не сам порядок словаря, поэтому переживёт рефакторинг,
    # если он сохранит корректность.
    assert lumen_formatting._scrub_latex(r"a \leq b \le c") == "a ≤ b ≤ c"
    assert lumen_formatting._scrub_latex(r"a \geq b \ge c") == "a ≥ b ≥ c"
    assert lumen_formatting._scrub_latex(r"a \neq b \ne c") == "a ≠ b ≠ c"
    assert lumen_formatting._scrub_latex(r"x \in S, \infty") == "x ∈ S, ∞"


def test_scrub_latex_protected_inside_code_blocks_via_full_pipeline():
    # Полный конвейер _md_to_html извлекает код ДО вызова _scrub_latex — обратные
    # слэши в реальном коде (regex, пути Windows) не должны пострадать.
    text = "```python\nimport re\npattern = re.compile(r\"\\d+\")\n```\nформула \\(\\pi r^2\\) вне кода."
    result = lumen_formatting._md_to_html(text)
    assert "\\d+" in result
    assert "π r²" in result


# ─────────────────── нормализация маркеров списков "- "/"* " → "• " ───────────────────
# Регрессия на реальный найденный пробел: _md_to_html конвертирует **bold**/*italic*/
# `code`/таблицы, но раньше НЕ трогал обычные markdown-списки — они уходили в
# Telegram буквально с "-"/"*" в начале строки.

@pytest.mark.parametrize(("source", "expected"), [
    ("- Пункт один\n- Пункт два", "• Пункт один\n• Пункт два"),
    ("* Пункт один\n* Пункт два", "• Пункт один\n• Пункт два"),
    ("  - вложенный пункт", "  • вложенный пункт"),
])
def test_normalize_bullet_markers(source, expected):
    assert lumen_formatting._normalize_bullet_markers(source) == expected


def test_normalize_bullet_markers_does_not_touch_bold_at_line_start():
    text = "**Жирный заголовок в начале строки**\nобычный текст"
    assert lumen_formatting._normalize_bullet_markers(text) == text


def test_normalize_bullet_markers_does_not_touch_table_separator_row():
    # Строка-разделитель таблицы ("---|---") не должна ошибочно приниматься за
    # маркер списка — у неё нет пробела сразу после первого дефиса.
    text = "Название | Цена\n---|---\nКофе | 150"
    assert lumen_formatting._normalize_bullet_markers(text) == text


def test_split_inline_bullets_splits_long_run_into_lines():
    # Прод 22.09.2026: модель написала весь список сравнения в один абзац через "•".
    items = ["пункт %d с развёрнутым текстом для набора длины абзаца" % i for i in range(5)]
    text = "Вот моменты сравнения характеристик: " + " • ".join(items)
    assert len(text) >= 200
    result = lumen_formatting._split_inline_bullets(text)
    lines = result.split("\n")
    assert lines[0] == "Вот моменты сравнения характеристик: " + items[0]
    assert lines[1:] == ["• " + item for item in items[1:]]


def test_split_inline_bullets_ignores_short_prose():
    # Короткие "чай • кофе" и два разделителя — обычная проза, не список.
    assert lumen_formatting._split_inline_bullets("На выбор чай • кофе • сок.") == "На выбор чай • кофе • сок."
    assert lumen_formatting._split_inline_bullets("Плюсы • минусы") == "Плюсы • минусы"
    assert lumen_formatting._split_inline_bullets("На выбор чай • кофе.") == "На выбор чай • кофе."


@pytest.mark.parametrize(("text", "n_lines", "intro"), [
    # Прод 26.09.2026: ровно 3 пункта (2 разделителя) — самый частый живой случай.
    (
        "• Рассеяние Рэлея: мелкие частицы в атмосфере рассеивают коротковолновый свет. "
        "• Солнечный спектр: солнце излучает больше синего света днём и ночью. "
        "• Отсутствие поглощения: газы атмосферы почти не поглощают синий свет.",
        3, None,
    ),
    # Прод 05.10.2026: склейка вообще без пробелов вокруг "•".
    (
        "• Отвечать на вопросы, вести беседу.• Выполнять веб-поиск и использовать полученные данные. "
        "•Читать открытые веб-страницы и анализировать их содержание.• Анализировать присланные фото, видео и документы.",
        4, None,
    ),
    # Та же склейка маркером "·": другой символ, та же каша для читателя.
    (
        "Итоги такие: первый пункт с подробностями для длины строки и смыслом · второй пункт с подробностями "
        "для длины строки и смыслом · третий пункт с подробностями для длины строки и смыслом · четвёртый пункт.",
        4, "Итоги такие: ",
    ),
])
def test_split_inline_bullets_splits_prod_answers(text, n_lines, intro):
    assert len(text) >= 200
    lines = lumen_formatting._split_inline_bullets(text).split("\n")
    assert len(lines) == n_lines
    assert "" not in lines
    if intro is None:
        assert all(line.startswith("• ") for line in lines)
    else:
        assert lines[0].startswith(intro)
        assert all(line.startswith("• ") for line in lines[1:])


def test_md_to_html_splits_inline_bullets_end_to_end():
    items = ["тезис номер %d с подробным раскрытием мысли" % i for i in range(4)]
    text = "Итоги сравнения моделей: " + " • ".join(items)
    result = lumen_formatting._md_to_html(text)
    assert result.count("\n• ") == 3


@pytest.mark.parametrize(("text", "intro"), [
    # Прод 25.09.2026: модель написала "1. ... 2. ... 3. ..." одним абзацем.
    (
        "1. Рэлеевское рассеяние — молекулы воздуха рассеивают солнечный свет. "
        "2. Зависимость от длины волны — короткие волны рассеиваются сильнее. "
        "3. Восприятие глаза — глаз чувствительнее к синему цвету неба.",
        None,
    ),
    (
        "Причины такие: 1. Первая причина с длинным пояснением текста. 2. Вторая причина с длинным пояснением текста. 3. Третья причина с длинным пояснением текста.",
        "Причины такие:",
    ),
])
def test_split_inline_numbered_splits_glued_lists(text, intro):
    lines = lumen_formatting._split_inline_numbered(text).split("\n")
    if intro is None:
        assert len(lines) == 3
        assert lines[0].startswith("1. ") and lines[1].startswith("2. ") and lines[2].startswith("3. ")
    else:
        assert lines[0] == intro
        assert [line[:2] for line in lines[1:]] == ["1.", "2.", "3."]


def test_split_inline_numbered_ignores_prose_and_versions():
    # Два пункта без третьего, годы/версии, отсылка "пункты 1. и 2." — не списки.
    assert lumen_formatting._split_inline_numbered("1. Да 2. Нет") == "1. Да 2. Нет"
    assert lumen_formatting._split_inline_numbered("Версия 3.5 вышла в 1995 году.") == "Версия 3.5 вышла в 1995 году."
    assert lumen_formatting._split_inline_numbered("Смотри пункты 1. и 2. ниже.") == "Смотри пункты 1. и 2. ниже."
    code = "```\n1. Первый шаг алгоритма. 2. Второй шаг алгоритма. 3. Третий шаг алгоритма.\n```"
    assert "1. Первый шаг алгоритма. 2." in lumen_formatting._md_to_html(code)


def test_md_to_html_splits_inline_numbered_end_to_end():
    text = "1. Пункт первый с достаточным пояснением для проверки. 2. Пункт второй с достаточным пояснением для проверки. 3. Пункт третий с достаточным пояснением для проверки."
    result = lumen_formatting._md_to_html(text)
    assert "\n2. " in result and "\n3. " in result


def test_md_to_rich_html_splits_inline_lists_end_to_end():
    # Rich-путь раньше не разносил слипшееся вообще (прод 25.09.2026: мелодрамы и вердикт одной строкой).
    bullets = "Вот несколько хороших мелодрамм: " + " • ".join(
        ["фильм номер %d с тёплым и подробным описанием сюжета" % i for i in range(4)]
    )
    assert "\n• " in lumen_formatting._md_to_rich_html(bullets)
    numbered = "1. Пункт первый с достаточным пояснением для проверки. 2. Пункт второй с достаточным пояснением для проверки. 3. Пункт третий с достаточным пояснением для проверки."
    rich = lumen_formatting._md_to_rich_html(numbered)
    assert "\n2. " in rich and "\n3. " in rich


def test_md_to_rich_html_scrubs_stray_latex_like_the_plain_path():
    # Регрессия (аудит 26.09.2026): rich-путь оставлял сырой LaTeX вне разделителей
    # ("\frac", "\alpha\times"), обычный переводил в юникод — один и тот же ответ
    # выглядел по-разному в стриме и в финале.
    b = chr(92)
    html = lumen_formatting._md_to_html(f"S = {b}frac{{1}}{{2}}{b}pi r^2 and {b}alpha{b}times{b}beta")
    rich = lumen_formatting._md_to_rich_html(f"S = {b}frac{{1}}{{2}}{b}pi r^2 and {b}alpha{b}times{b}beta")
    assert html == rich
    assert b not in rich, "сырые слэши в финальном сообщении — провал промта"
    assert "1/2" in rich and "α" in rich


def test_md_to_rich_html_keeps_delimited_math_untouched():
    # $...$ уходит в <tg-math> как есть (это единственное намеренное отличие rich
    # от обычного пути, где та же формула становится юникодом).
    rich = lumen_formatting._md_to_rich_html("скорость $x^2$ тут")
    assert "<tg-math>x^2</tg-math>" in rich


def test_render_paths_parity_on_shared_cases():
    # Параметрический тест на паритет двух путей рендера (обычный HTML и rich):
    # расходиться могут только заголовки ($x$ → <tg-math>), всё остальное обязано
    # совпадать, иначе стрим и финальное сообщение показывают разное.
    b = chr(92)
    shared = [
        "Просто текст без разметки",
        "**жирно** и *курсив* и `код`",
        "- пункт один" + "\n" + "- пункт два",
        "1. раз" + "\n" + "2. два",
        "> цитата тут",
        "текст с <b>сырым html</b>",
        f"S = {b}pi r^2 и {b}sqrt{{9}}",
        "список: " + " • ".join(f"пункт {i} с текстом подлиннее" for i in range(4)),
    ]
    for case in shared:
        assert lumen_formatting._md_to_html(case) == lumen_formatting._md_to_rich_html(case), case


def test_render_paths_differ_only_for_headings():
    # Заголовки — единственное намеренное расхождение: rich умеет <h3>, обычный
    # путь отдаёт <b>. Проверяем, что это всё, что разъезжается.
    assert lumen_formatting._md_to_html("## Title") == "<b>Title</b>"
    assert lumen_formatting._md_to_rich_html("## Title") == "<h3>Title</h3>"


def test_md_to_rich_html_splits_three_bullets_prod_movies():
    # Прод 26.09.2026: ответ про мелодрамы — 3 пункта склеились, rich-путь молчал.
    text = (
        "• Амелі (2001): французская камедыя-фэнтэзі пра маладых, якія бачаць свет у яркіх фарбах. "
        "• Падынгтан (2014): лёгкі сямейны фільм пра мілога мядзведзя, які шукае новы дом у Лондане. "
        "• Crazy Rich Asians (2018): вясёлая рамантычная камедыя пра кітайскую эліту, поўная колеру і музыкі."
    )
    assert len(text) >= 200
    assert "\n• " in lumen_formatting._md_to_rich_html(text)


def test_md_to_html_full_pipeline_converts_bullet_list_with_bold():
    text = "* **Возмездие:** аргумент про справедливость\n* **Сдерживание:** снижает преступность"
    result = lumen_formatting._md_to_html(text)
    assert result.startswith("• <b>Возмездие:</b>")
    assert "\n• <b>Сдерживание:</b>" in result
    assert "*" not in result.replace("</b>", "").replace("<b>", "")


# ─────────────── markdown-заголовки "#"/"##"/"###" → **жирный текст** ───────────────
# РЕГРЕССИЯ, найденная на реальных скриншотах Telegram (18 августа 2026):
# system_prompt.py запрещает markdown-заголовки, но модели (особенно бесплатные
# модели OpenRouter) регулярно их всё равно пишут — раньше "###" уходило в
# Telegram буквально, без единой защитной сетки (в отличие от таблиц/списков).

@pytest.mark.parametrize(("source", "expected"), [
    ("### Как она выводится?", "**Как она выводится?**"),
    ("# Заголовок", "**Заголовок**"),
    ("## Подзаголовок", "**Подзаголовок**"),
])
def test_normalize_headers_converts_atx_to_bold(source, expected):
    assert lumen_formatting._normalize_headers(source) == expected


def test_normalize_headers_does_not_touch_hash_mid_line():
    # "C#" / "#tag" не в начале строки — ATX-заголовок ТОЛЬКО в начале строки.
    text = "Язык C# отличается от C++.\nПодробнее: #tag"
    assert lumen_formatting._normalize_headers(text) == text


def test_normalize_headers_requires_space_after_hashes():
    # "#без_пробела" — не заголовок по правилам CommonMark ATX, не трогаем.
    assert lumen_formatting._normalize_headers("#без_пробела текст") == "#без_пробела текст"


def test_normalize_headers_bare_hashes_with_no_text_removed():
    assert lumen_formatting._normalize_headers("### \nследующая строка") == "\nследующая строка"


def test_normalize_headers_does_not_double_wrap_already_bold_content():
    # "### **Важно**" — модель сама уже обернула текст в bold; повторная обёртка
    # дала бы "****Важно****" и сломала бы парность "**" в Phase 3.
    assert lumen_formatting._normalize_headers("### **Важно**") == "**Важно**"


def test_md_to_html_full_pipeline_strips_stray_header_markers():
    # Регрессия на реальный найденный баг: пользователь видел буквальные "###" в
    # сообщении бота вместо жирного текста — полный конвейер должен это исправлять.
    result = lumen_formatting._md_to_html("### Как она выводится?\nобычный текст")
    assert "###" not in result
    assert result.startswith("<b>Как она выводится?</b>")


def test_md_to_html_header_hash_inside_code_block_untouched():
    # "#" внутри блока кода (например, комментарий Python) не должен считаться
    # заголовком — код уже вынесен плейсхолдером до этой фазы.
    text = "```python\n# обычный комментарий\nprint(1)\n```"
    result = lumen_formatting._md_to_html(text)
    assert "# обычный комментарий" in result
    assert "<b>" not in result


# ─────────────────── spoiler-тег: защитная сетка (тот же принцип, что <u>) ───────────────────

@pytest.mark.parametrize(("source", "expected"), [
    ("<tg-spoiler>секрет</tg-spoiler>", "секрет"),
    ('<span class="tg-spoiler">секрет</span>', "секрет"),
])
def test_md_to_html_strips_literal_spoiler_tags(source, expected):
    assert lumen_formatting._md_to_html(source) == expected


# ─────────────────── подсветка синтаксиса: язык из ```fence сохраняется ───────────────────
# (сам кейс с языком покрыт test_md_to_html_code_block выше)

def test_md_to_html_code_block_without_language_unchanged():
    assert lumen_formatting._md_to_html("```\nprint(1)\n```") == "<pre>print(1)</pre>"


# ─────────────────── markdown-цитаты "> " → <blockquote> ───────────────────

@pytest.mark.parametrize(("source", "expected"), [
    ("> цитата", "<blockquote>цитата</blockquote>"),
    ("> первая строка\n> вторая строка", "<blockquote>первая строка\nвторая строка</blockquote>"),
])
def test_md_to_html_converts_blockquotes(source, expected):
    assert lumen_formatting._md_to_html(source) == expected


def test_md_to_html_blockquote_markdown_inside_still_converts():
    result = lumen_formatting._md_to_html("> **важно**: не забудь")
    assert result == "<blockquote><b>важно</b>: не забудь</blockquote>"


def test_md_to_html_blockquote_only_affects_quoted_lines():
    result = lumen_formatting._md_to_html("обычный текст\n> цитата\nещё текст")
    assert result == "обычный текст\n<blockquote>цитата</blockquote>\nещё текст"


def test_md_to_html_no_false_positive_on_greater_than_sign():
    # "5 > 3" — обычное сравнение, не в начале строки — не должно стать цитатой.
    assert lumen_formatting._md_to_html("сравнение: 5 > 3") == "сравнение: 5 &gt; 3"


# ─────────────────── markdown-ссылки [текст](url) → <a href="url">текст</a> ───────────────────

def test_md_to_html_converts_markdown_link():
    result = lumen_formatting._md_to_html("[почитать здесь](https://example.com/page)")
    assert result == '<a href="https://example.com/page">почитать здесь</a>'


def test_md_to_html_link_with_underscores_in_url_not_corrupted_by_italic():
    # Регрессия: URL с двумя "_" мог бы ошибочно засчитаться за пару italic-
    # маркеров, если бы конвертация ссылок шла раньше bold/italic в Phase 3.
    result = lumen_formatting._md_to_html("[текст](https://example.com/foo_bar_baz)")
    assert result == '<a href="https://example.com/foo_bar_baz">текст</a>'
    assert "<i>" not in result


def test_md_to_html_link_text_markdown_not_processed_intentionally():
    # Текст ссылки вырезается плейсхолдером в Phase 1 (см. комментарий в коде) —
    # markdown внутри него намеренно не поддерживается (не запрашивалось), выходит
    # как обычный экранированный текст, а не корёжится и не превращается в <b>.
    result = lumen_formatting._md_to_html("[**жирная ссылка**](https://example.com)")
    assert result == '<a href="https://example.com">**жирная ссылка**</a>'


def test_md_to_html_non_http_bracket_text_left_alone():
    # "[note]" без http(s)-ссылки — не markdown-ссылка, не должно превращаться в <a>.
    assert lumen_formatting._md_to_html("текст [note] продолжение") == "текст [note] продолжение"


def test_md_to_html_link_url_with_quote_is_escaped():
    result = lumen_formatting._md_to_html('[текст](https://example.com/"injected)')
    assert '&quot;' in result
    assert '"injected' not in result


# ─────────────────── _md_to_rich_html (Bot API 10.1+, sendRichMessage) ───────────────────
# Тот же конвейер, что _md_to_html, но таблицы/заголовки/LaTeX идут настоящими
# рич-тегами — сервер Telegram рендерит их сам. Тесты фиксируют контракт,
# от которого зависит rich-отправка в bot.py.

def test_rich_table_renders_bordered_table():
    result = lumen_formatting._md_to_rich_html("| A | B |\n|---|---|\n| 1 | 2 |")
    assert result == "<table bordered><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"


def test_rich_table_cells_support_inline_markup_and_escape():
    result = lumen_formatting._md_to_rich_html("| H | K |\n|---|---|\n| **b** <x> | 2 |")
    assert result == "<table bordered><tr><th>H</th><th>K</th></tr><tr><td><b>b</b> &lt;x&gt;</td><td>2</td></tr></table>"


def test_rich_non_table_pipes_left_alone():
    # Одиночные "|" без строки-разделителя — не таблица, текст как есть.
    assert lumen_formatting._md_to_rich_html("a | b") == "a | b"


@pytest.mark.parametrize(("source", "expected"), [
    ("## Заголовок", "<h3>Заголовок</h3>"),
    ("### Подзаголовок", "<h4>Подзаголовок</h4>"),
    # Тот же допуск, что у легаси-нормализации: ведущие пробелы и 1–6 решёток.
    ("   ## Заголовок", "<h3>Заголовок</h3>"),
    ("# Топ", "<h2>Топ</h2>"),
    ("#### Глубокий", "<h4>Глубокий</h4>"),
])
def test_rich_headings_become_h_tags(source, expected):
    assert lumen_formatting._md_to_rich_html(source) == expected


@pytest.mark.parametrize(("source", "expected"), [
    ("корень $x^2$ тут", "корень <tg-math>x^2</tg-math> тут"),
    ("формула $$E=mc^2$$ конец", "формула <tg-math-block>E=mc^2</tg-math-block> конец"),
    # Скобочные формы LaTeX (реальный кейс nemotron) извлекаются раньше $.
    ("смотри \\[S = \\pi r^{2}\\] конец", "смотри <tg-math-block>S = \\pi r^{2}</tg-math-block> конец"),
    ("значение \\(x\\) тут", "значение <tg-math>x</tg-math> тут"),
])
def test_rich_math_preserved_as_tg_math_tags(source, expected):
    # В отличие от _md_to_html (юникод-замена), рич-путь отдаёт LaTeX сырым —
    # рендерит сервер Telegram.
    assert lumen_formatting._md_to_rich_html(source) == expected


def test_rich_keeps_bold_links_code_quotes_and_escape():
    assert lumen_formatting._md_to_rich_html("**b**") == "<b>b</b>"
    assert lumen_formatting._md_to_rich_html("[t](https://example.com/a_b)") == '<a href="https://example.com/a_b">t</a>'
    assert lumen_formatting._md_to_rich_html("`<x>`") == "<code>&lt;x&gt;</code>"
    assert lumen_formatting._md_to_rich_html("5 < 10") == "5 &lt; 10"
    assert lumen_formatting._md_to_rich_html("> цитата") == "<blockquote>цитата</blockquote>"


def test_rich_math_inside_code_stays_code():
    # LaTeX внутри кода — код, а не формула (порядок экстракции: код раньше математики).
    assert lumen_formatting._md_to_rich_html("`$x$`") == "<code>$x$</code>"


def test_br_tag_becomes_line_break_in_both_paths():
    # Прод-кейс 17.09.2026: модель пишет "<br>" внутри ячеек таблиц — без
    # обработки долетает до escape и светится буквально ("...сгор.<br>Pro Max").
    assert lumen_formatting._md_to_html("a<br>b") == "a\nb"
    assert lumen_formatting._md_to_html("a<br/>b") == "a\nb"
    assert lumen_formatting._md_to_rich_html("| H | K |\n|---|---|\n| a<br>b | 2 |") == (
        "<table bordered><tr><th>H</th><th>K</th></tr><tr><td>a<br/>b</td><td>2</td></tr></table>"
    )
    # "<blockquote>" не должен съедаться строгим паттерном <br>.
    assert "blockquote" in lumen_formatting._md_to_html("<blockquote>ц</blockquote>")


def test_strip_markdown_removes_syntax_keeping_words():
    assert lumen_formatting._strip_markdown("**жирный** и *курсив*") == "жирный и курсив"
    assert lumen_formatting._strip_markdown("код `x=1` тут") == "код x=1 тут"
    assert lumen_formatting._strip_markdown("[текст](https://example.com)") == "текст"
    assert lumen_formatting._strip_markdown("## Заголовок") == "Заголовок"
    assert lumen_formatting._strip_markdown("~~чист~~") == "чист"
    assert lumen_formatting._strip_markdown("") == ""
    assert lumen_formatting._strip_markdown("обычный текст 5 < 10") == "обычный текст 5 < 10"


def test_rich_prices_are_not_treated_as_math():
    # "$50 до $100" — контент с пробелом у границы, не формула. Одиночный знак
    # без пары — тоже текст.
    assert lumen_formatting._md_to_rich_html("от $50 до $100") == "от $50 до $100"
    assert lumen_formatting._md_to_rich_html("всего $80 000") == "всего $80 000"
    assert lumen_formatting._md_to_rich_html("$x$") == "<tg-math>x</tg-math>"


def test_inline_splitters_leave_structural_lines_alone():
    # Регрессия (враждебное ревью 27.09.2026): разнос слипшихся списков ел цитаты,
    # заголовки и строки markdown-таблиц — пользователь видел пустую плашку цитаты,
    # торчащее "##" и разорванную таблицу.
    quoted = "> 1. alpharazat elfin uvicorn 2. betakakt elfin uvicorn 3. gamakakt elfin uvicorn"
    out = lumen_formatting._md_to_html(quoted)
    assert out.startswith("<blockquote>") and out.endswith("</blockquote>")
    assert "1." in out, f"пункты должны остаться внутри цитаты: {out!r}"
    assert "1." in lumen_formatting._md_to_rich_html(quoted)

    head = "## 1. alpharazat elfin uvicorn 2. betakakt elfin uvicorn 3. gamakakt elfin uvicorn"
    assert "##" not in lumen_formatting._md_to_html(head)
    rich_head = lumen_formatting._md_to_rich_html(head)
    assert rich_head.startswith("<h3>") and "\n" not in rich_head, rich_head

    table = "| A | B |\n|---|---|\n| 1. alpharazat elfin uvicorn 2. betakakt elfin uvicorn 3. gamakakt elfin uvicorn | four |"
    assert "<b>A:</b>" in lumen_formatting._md_to_html(table), "разнос разорвал строку таблицы"
    rich_table = lumen_formatting._md_to_rich_html(table)
    assert "<table" in rich_table and "<td>1. alpharazat elfin uvicorn 2. betakakt elfin uvicorn 3. gamakakt elfin uvicorn</td>" in rich_table

    # Таблица без внешних пайпов: строка не начинается с `|`, но разносчик обязан
    # узнать блок таблицы, иначе HTML-путь рвал её до детекта, а rich — нет.
    bare_table = "Name | Age\n---|---\n1. alpharazat elfin uvicorn 2. betakakt elfin uvicorn 3. gamakakt elfin uvicorn | four"
    html_bare = lumen_formatting._md_to_html(bare_table)
    assert "<b>Name:</b>" in html_bare and "---" not in html_bare, html_bare
    rich_bare = lumen_formatting._md_to_rich_html(bare_table)
    assert "<table" in rich_bare and "1. alpharazat elfin uvicorn 2. betakakt elfin uvicorn 3. gamakakt elfin uvicorn" in rich_bare

    # Буллеты тоже не лезут в служебные строки (длина обязана превышать порог 200).
    long_items = " • ".join(f"пункт номер {i} с достаточно длинным описанием" for i in range(4))
    for structural in (f"> вводная {long_items}", f"## вводная {long_items}", f"| вводная {long_items} | конец |"):
        assert lumen_formatting._split_inline_bullets(structural) == structural, structural[:60]
    long_numbered = "1. alpharazat elfin uvicorn 2. betakakt elfin uvicorn 3. gamakakt elfin uvicorn"
    for structural in (f"> {long_numbered}", f"## {long_numbered}"):
        assert "\n" not in lumen_formatting._split_inline_numbered(structural), structural[:60]


def test_inline_splitters_still_split_prose():
    # Граница правки: обычная проза по-прежнему разносится.
    prose = "1. alpharazat elfin uvicorn 2. betakakt elfin uvicorn 3. gamakakt elfin uvicorn"
    assert lumen_formatting._split_inline_numbered(prose).count("\n") == 2
    assert lumen_formatting._split_inline_bullets(prose) == prose


def test_scrub_latex_int_is_not_eaten_by_in():
    # Регрессия (враждебное ревью 27.09.2026): в карте символов \in шёл раньше \int,
    # поэтому "\int_0^1" превращался в "∈t₀¹".
    assert lumen_formatting._scrub_latex(r"\int_0^1 x dx") == "∫₀¹ x dx"
    assert lumen_formatting._scrub_latex(r"a \in b") == "a ∈ b"
    assert lumen_formatting._scrub_latex(r"\infty") == "∞"


def test_rich_heading_keeps_scrubbed_latex():
    # Регрессия (враждебное ревью 27.09.2026): заголовок уходил в плейсхолдер ДО
    # общего _scrub_latex, поэтому "\alpha" оставался сырым в <h3>.
    out = lumen_formatting._md_to_rich_html("## Коэффициент \\alpha и \\int_0^1")
    assert "\\alpha" not in out and "\\int" not in out
    assert "α" in out and "∫" in out
