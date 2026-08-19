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



# ─────────────────────────── _md_to_html ───────────────────────────

def test_md_to_html_empty_string():
    assert lumen_formatting._md_to_html("") == ""


def test_md_to_html_bold():
    assert lumen_formatting._md_to_html("**bold**") == "<b>bold</b>"


def test_md_to_html_italic():
    assert lumen_formatting._md_to_html("*italic*") == "<i>italic</i>"


def test_md_to_html_strikethrough():
    assert lumen_formatting._md_to_html("~~strike~~") == "<s>strike</s>"


def test_md_to_html_escapes_raw_html():
    # Сырой HTML от модели не должен пролезть как есть — иначе Telegram
    # либо сломает parse_mode=HTML, либо (хуже) отрендерит чужую разметку.
    assert lumen_formatting._md_to_html("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_md_to_html_inline_code():
    assert lumen_formatting._md_to_html("`code`") == "<code>code</code>"


def test_md_to_html_code_block():
    # Язык фенса теперь сохраняется как class="language-x" (подсветка синтаксиса
    # в Telegram) — см. test_md_to_html_code_block_preserves_language_for_syntax_
    # highlighting ниже; без языка поведение как раньше (test_md_to_html_code_
    # block_without_language_unchanged).
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


def test_md_to_html_converts_markdown_table_to_bullet_list():
    # Регрессия на реальный найденный при тестировании баг: Telegram не рендерит
    # markdown-таблицы НИ В КАКОМ режиме — пользователь видел сырой текст с "|" и
    # "---" вместо аккуратной таблицы (подтверждено скриншотами реального теста).
    text = (
        "| Аспект | React | Vue |\n"
        "|--------|-------|-----|\n"
        "| Кривая обучения | Высокая | Низкая |\n"
        "| Сообщество | Огромное | Среднее |"
    )
    result = lumen_formatting._md_to_html(text)
    assert "|" not in result
    assert "<b>Аспект:</b> Кривая обучения" in result
    assert "<b>React:</b> Высокая" in result
    assert "<b>Vue:</b> Низкая" in result
    assert result.count("•") == 2


def test_md_to_html_table_without_outer_pipes_still_converted():
    # Некоторые модели пишут таблицы без внешних "|" по краям строки.
    text = (
        "Название | Цена\n"
        "---|---\n"
        "Кофе | 150\n"
        "Чай | 100"
    )
    result = lumen_formatting._md_to_html(text)
    assert "|" not in result
    assert "<b>Название:</b> Кофе" in result
    assert "<b>Цена:</b> 150" in result


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

def test_scrub_latex_converts_bracket_delimiters_and_pi_and_superscript():
    result = lumen_formatting._scrub_latex(r"Площадь: \[ S = \pi r^{2} \]")
    assert "\\[" not in result and "\\]" not in result
    assert "π" in result
    assert "r²" in result


def test_scrub_latex_converts_paren_delimiters():
    result = lumen_formatting._scrub_latex(r"формула \(x^{2}+y^{2}=r^{2}\)")
    assert "\\(" not in result and "\\)" not in result
    assert "x²+y²=r²" in result


def test_scrub_latex_converts_frac_and_sqrt():
    assert lumen_formatting._scrub_latex(r"\frac{1}{2}") == "1/2"
    assert lumen_formatting._scrub_latex(r"\sqrt{16}") == "√16"


def test_scrub_latex_converts_common_symbols():
    result = lumen_formatting._scrub_latex(r"\times \pm \leq \geq \infty \sum \int")
    for leftover in ("\\times", "\\pm", "\\leq", "\\geq", "\\infty", "\\sum", "\\int"):
        assert leftover not in result
    assert "×" in result and "±" in result and "≤" in result and "≥" in result and "∞" in result


def test_scrub_latex_noop_when_no_backslash_or_dollar():
    assert lumen_formatting._scrub_latex("обычный текст без формул") == "обычный текст без формул"


def test_scrub_latex_does_not_confuse_currency_with_math_delimiters():
    # РЕГРЕССИЯ, найденная при code-review (25 июля 2026): первая версия скрубера
    # обрабатывала и одиночный "$...$" как инлайн-LaTeX. Если в одном сообщении
    # встречались и сумма в долларах, и настоящая формула ("цена $100, а формула
    # $x^2$ рядом"), первый "$" суммы ошибочно спаривался с первым "$" формулы —
    # результат был ХУЖЕ исходного: обрезанные суммы плюс осиротевший "$" в хвосте
    # ("цена 100, а формула x²$ рядом"). Одиночный "$" теперь не обрабатывается
    # вообще — только "$$...$$". Сами доллары остаются нетронутыми в обоих случаях;
    # "x^2" внутри всё равно аккуратно превращается в "x²" — это отдельная, не
    # завязанная на "$"-разделители замена (см. следующий тест), она безвредна и
    # здесь, и вне контекста "$".
    assert lumen_formatting._scrub_latex("цена $100, а формула $x^2$ рядом") == "цена $100, а формула $x²$ рядом"
    assert lumen_formatting._scrub_latex("первый вариант — $50, второй — $100") == "первый вариант — $50, второй — $100"
    assert lumen_formatting._scrub_latex("стоимость: $100. Итого: $200.") == "стоимость: $100. Итого: $200."


def test_scrub_latex_still_converts_double_dollar_display_math():
    assert lumen_formatting._scrub_latex("$$x^2 + y^2$$") == "x² + y²"


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

def test_normalize_bullet_markers_converts_dash_and_asterisk():
    assert lumen_formatting._normalize_bullet_markers("- Пункт один\n- Пункт два") == "• Пункт один\n• Пункт два"
    assert lumen_formatting._normalize_bullet_markers("* Пункт один\n* Пункт два") == "• Пункт один\n• Пункт два"


def test_normalize_bullet_markers_preserves_indentation():
    assert lumen_formatting._normalize_bullet_markers("  - вложенный пункт") == "  • вложенный пункт"


def test_normalize_bullet_markers_does_not_touch_bold_at_line_start():
    text = "**Жирный заголовок в начале строки**\nобычный текст"
    assert lumen_formatting._normalize_bullet_markers(text) == text


def test_normalize_bullet_markers_does_not_touch_table_separator_row():
    # Строка-разделитель таблицы ("---|---") не должна ошибочно приниматься за
    # маркер списка — у неё нет пробела сразу после первого дефиса.
    text = "Название | Цена\n---|---\nКофе | 150"
    assert lumen_formatting._normalize_bullet_markers(text) == text


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

def test_normalize_headers_converts_h3_to_bold():
    assert lumen_formatting._normalize_headers("### Как она выводится?") == "**Как она выводится?**"


def test_normalize_headers_converts_h1_and_h2():
    assert lumen_formatting._normalize_headers("# Заголовок") == "**Заголовок**"
    assert lumen_formatting._normalize_headers("## Подзаголовок") == "**Подзаголовок**"


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

def test_md_to_html_strips_literal_spoiler_tag():
    assert lumen_formatting._md_to_html("<tg-spoiler>секрет</tg-spoiler>") == "секрет"


def test_md_to_html_strips_literal_span_spoiler_tag():
    assert lumen_formatting._md_to_html('<span class="tg-spoiler">секрет</span>') == "секрет"


# ─────────────────── подсветка синтаксиса: язык из ```fence сохраняется ───────────────────

def test_md_to_html_code_block_preserves_language_for_syntax_highlighting():
    result = lumen_formatting._md_to_html("```python\nprint(1)\n```")
    assert result == '<pre><code class="language-python">print(1)</code></pre>'


def test_md_to_html_code_block_without_language_unchanged():
    assert lumen_formatting._md_to_html("```\nprint(1)\n```") == "<pre>print(1)</pre>"


# ─────────────────── markdown-цитаты "> " → <blockquote> ───────────────────

def test_md_to_html_converts_single_line_blockquote():
    assert lumen_formatting._md_to_html("> цитата") == "<blockquote>цитата</blockquote>"


def test_md_to_html_converts_multiline_blockquote_as_one_block():
    result = lumen_formatting._md_to_html("> первая строка\n> вторая строка")
    assert result == "<blockquote>первая строка\nвторая строка</blockquote>"


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
