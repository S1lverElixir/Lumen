"""Unit tests for lumen_formatting.py: converting Lumen's markdown-like text to Telegram HTML (_md_to_html), guarding against raw LaTeX (_scrub_latex), and normalizing list markers (_normalize_bullet_markers).

Part of the split of test_bot_helpers.py by module, following the existing source split (lumen_formatting.py / lumen_security.py / lumen_router_config.py / bot.py) — see README and the technical debt audit. lumen_formatting.py has no dependencies on Telegram/Gemini/OpenRouter or bot runtime state, so this file tests it directly (import lumen_formatting), without importing bot.py — it is not tied to the BOT_TOKEN/GEMINI_API_KEY environment variables that conftest.py sets only for bot.py itself.

Run:
    pytest test_lumen_formatting.py -v
"""
import lumen_formatting
import pytest
import time



# ─────────────────────────── _truncate_html_to_fit ───────────────────────────

def test_truncate_html_to_fit_keeps_short_text_and_cuts_at_source_boundary():
    # Regression AUD-J-002: cutting ready HTML broke tags ("can't parse entities").
    # Moved from test_bot.py to follow the function (TG_MAX_LEN=4096 is Telegram's limit).
    short = lumen_formatting._truncate_html_to_fit("привет", 4096)
    assert short == lumen_formatting._md_to_html("привет")
    long_md = "**" + "x" * 5000 + "**"
    cut = lumen_formatting._truncate_html_to_fit(long_md, 4096)
    assert len(cut) <= 4096
    assert cut.endswith("…")
    assert cut.count("<b>") == cut.count("</b>")
    # Telegram's limit is measured in UTF-16 units (emoji = 2): slicing by len()
    # returned HTML longer than the limit and 400 on a guest response, where there is no fallback
    # to plain text (corpus check 05.11.2026).
    emoji_md = " ".join(["😀 текст"] * 200)
    cut_units = lumen_formatting._truncate_html_to_fit(emoji_md, 50)
    assert sum(2 if ord(ch) > 0xFFFF else 1 for ch in cut_units) <= 50


@pytest.mark.parametrize(("source", "expected"), [
    ("User Safety: safe\nResponse Safety: safe", ""),
    ("<|tool_call_start|>[google(query='x')]<|tool_call_end|>", ""),
    ("Привет\nUser Safety: safe", "Привет"),
])
def test_strip_provider_artifacts_removes_service_markers(source, expected):
    # Prod 05.11.2026 (backup sample): security classifier labels and
    # tool-call markup went to the chat as a complete answer.
    assert lumen_formatting._strip_provider_artifacts(source) == expected


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
    # Raw HTML from the model must not pass through as is — otherwise Telegram
    # either break parse_mode=HTML or, worse, render someone else markup.
    assert lumen_formatting._md_to_html("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_md_to_html_inline_code():
    assert lumen_formatting._md_to_html("`code`") == "<code>code</code>"


def test_md_to_html_code_block():
    # The fence language is preserved as class="language-x" for Telegram highlighting.
    assert lumen_formatting._md_to_html("```python\nprint(1)\n```") == '<pre><code class="language-python">print(1)</code></pre>'


def test_md_to_html_escapes_inside_code_block():
    # Code tags inside code/pre must also be escaped; otherwise a line like
    # "`<tag>`" would break the message's HTML markup in Telegram.
    assert lumen_formatting._md_to_html("`<tag>`") == "<code>&lt;tag&gt;</code>"


def test_md_to_html_no_html_injection_via_markdown_markers():
    # Regression test for a real incident: Markdown characters inside text
    # must not produce invalid HTML markup (unpaired tags).
    result = lumen_formatting._md_to_html("**bold** and *italic* and `code`")
    assert result.count("<b>") == result.count("</b>")
    assert result.count("<i>") == result.count("</i>")
    assert result.count("<code>") == result.count("</code>")


def test_table_separator_match_is_time_bounded():
    # A quadratic regex on whitespace tails hung: the string is cut before match.
    line = "| a | b |\n" + " " * 12000 + "-"
    started = time.monotonic()
    lumen_formatting._convert_markdown_tables_to_lists(line)
    assert time.monotonic() - started < 1.0
    # The ordinary delimiter is still detected.
    assert lumen_formatting._is_table_separator("|---|---|") is True
    assert lumen_formatting._is_table_separator(" --- | --- ") is True
    assert lumen_formatting._is_table_separator("просто текст") is False


def test_split_chunks_fit_html_and_utf16_units():
    # The limit is calculated on the final HTML in units: inflated markup and emoji
    # outside the BMP no longer cause a 400 from Telegram.
    bold_ru = "**" + "привет " * 600 + "**"
    chunks = lumen_formatting._split_text_chunks(bold_ru, 4096)
    assert len(chunks) > 1
    for chunk in chunks:
        assert lumen_formatting._tg_len(lumen_formatting._md_to_html(chunk)) <= 4096
    emoji = "😀" * 3000
    emoji_chunks = lumen_formatting._split_text_chunks(emoji, 4096)
    assert len(emoji_chunks) == 2
    for chunk in emoji_chunks:
        assert lumen_formatting._tg_len(chunk) <= 4096
    assert "".join(emoji_chunks) == emoji
    # Boundary: exactly 4096 units in one chunk; +1 emoji already requires two.
    assert len(lumen_formatting._split_text_chunks("😀" * 2048, 4096)) == 1
    assert len(lumen_formatting._split_text_chunks("😀" * 2049, 4096)) == 2
    assert len(lumen_formatting._split_text_chunks("я" * 4096, 4096)) == 1


def test_split_chunks_keep_fences_intact():
    # A long fence is split line by line with reopening: every chunk is rendered.
    code = "```python\n" + "x = 1\n" * 1500 + "```"
    chunks = lumen_formatting._split_text_chunks(code, 4096)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.startswith("```") and chunk.rstrip().endswith("```")
        assert "<pre>" in lumen_formatting._md_to_html(chunk)
        assert "```" not in lumen_formatting._md_to_html(chunk)


def test_split_chunks_merge_short_fenced_message_back():
    # A short message with code goes in one chunk, not three.
    text = "Hi\n```python\nx = 1\n```\nBye"
    chunks = lumen_formatting._split_text_chunks(text, 4096)
    assert chunks == [text]


def test_split_chunks_presplit_inline_bullets_before_cutting():
    # Production 07.10.2026: the chunker split a run-together list on ". " before the splitter,
    # chunks with 1 marker were not split, and junk reached the chat.
    text = (
        "Лучше проприетарные системы, когда: • Требуется гарантированная поддержка "
        "с длинным пояснением для набора длины строки. • Важна интеграция из коробки "
        "с длинным пояснением для набора длины строки. • Нужна безопасность за счет "
        "сокрытия с длинным пояснением для набора длины строки."
    )
    chunks = lumen_formatting._split_text_chunks(text, 4096)
    assert any("\n• " in c for c in chunks)


def test_md_to_html_link_with_inline_code_has_no_placeholder_leak():
    # Code inside the link label: the placeholder does not leak, and both tags are present.
    html = lumen_formatting._md_to_html("[`code` текст](https://example.com)")
    assert "\x00" not in html
    assert '<a href="https://example.com">' in html
    assert "<code>code</code>" in html


def test_strip_markdown_keeps_underscores_in_urls_and_filenames():
    # Underscores inside words are not emphasis: URLs, files, adjacent emojis.
    assert lumen_formatting._strip_markdown("см. https://example.com/my_file_name тут") == "см. https://example.com/my_file_name тут"
    assert lumen_formatting._strip_markdown("открой my_file_name.txt и отчёт_📄_финал.txt") == "открой my_file_name.txt и отчёт_📄_финал.txt"
    assert lumen_formatting._strip_markdown("файл_имя with-hyphen остаётся") == "файл_имя with-hyphen остаётся"
    # Real highlights are still stripped, including Cyrillic and emoji.
    assert lumen_formatting._strip_markdown("**жирно** и *курсив*") == "жирно и курсив"
    assert lumen_formatting._strip_markdown("привет _друг_ 😀") == "привет друг 😀"
    assert lumen_formatting._strip_markdown("*😀*") == "😀"


def test_md_to_html_normalizes_raw_html_bold_tag():
    # Regression: the model sometimes writes literal <b>/<i> tags instead of markdown
    # (despite the instruction in system_prompt.py) previously this was escaped
    # and was shown to the user as visible garbage like "<b>".
    assert lumen_formatting._md_to_html("<b>Заголовок</b>") == "<b>Заголовок</b>"


def test_md_to_html_strips_broken_self_closing_tag():
    # A real bug found: the model sometimes writes an invalid self-closing "<b/>".
    result = lumen_formatting._md_to_html("текст <b/> ещё текст")
    assert "<b/>" not in result
    assert "&lt;b/&gt;" not in result


def test_md_to_html_normalizes_raw_html_code_and_pre():
    assert lumen_formatting._md_to_html("код: <code>print(1)</code>") == "код: <code>print(1)</code>"
    result = lumen_formatting._md_to_html("<pre>def f():\n    pass</pre>")
    assert result.startswith("<pre>") and result.endswith("</pre>")


def test_md_to_html_still_escapes_ordinary_comparison_operators():
    # Verify that tag normalization did not break ordinary escaping:
    # '5 < 10' must not turn into an unclosed tag.
    assert lumen_formatting._md_to_html("сравнение: 5 < 10") == "сравнение: 5 &lt; 10"


@pytest.mark.parametrize(("text", "present"), [
    # Regression: Telegram does not render markdown tables in any mode —
    # user saw raw text with '|' and '---' (confirmed by screenshots).
    (
        "| Аспект | React | Vue |\n"
        "|--------|-------|-----|\n"
        "| Кривая обучения | Высокая | Низкая |\n"
        "| Сообщество | Огромное | Среднее |",
        ("<b>Аспект:</b> Кривая обучения", "<b>React:</b> Высокая", "<b>Vue:</b> Низкая"),
    ),
    # Some models write tables without outer "|" characters at the row edges.
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
    # "|" inside a code block (for example, bitwise OR in Rust/C) must not
    # be wrongly recognized as a table — the code was already moved in the previous step.
    text = "```rust\nlet x = a | b;\nlet y = c | d;\n```"
    result = lumen_formatting._md_to_html(text)
    assert "let x = a | b;" in result
    assert "•" not in result


def test_md_to_html_no_false_positive_on_plain_text_with_dashes():
    # Ordinary text with hyphens (not a table) must not break the converter.
    text = "Список дел:\n- сходить в магазин\n- купить хлеб"
    result = lumen_formatting._md_to_html(text)
    assert "сходить в магазин" in result
    assert "купить хлеб" in result


# ─────────────────── LaTeX scrubber (safety net for raw LaTeX) ───────────────────
# Fallback is disabled on this task: nemotron-3-nano-30b-a3b:free
# returned "\[ S = \pi r^{2}, \]" and "\(x^{2}+y^{2}=r^{2}\)" instead of Unicode, despite
# an explicit LaTeX prohibition in system_prompt.py.

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
    # REGRESSION found during code review (25.07.2026): the first version of the scrubber
    # handled even a single "$...$" as inline LaTeX. If one message
    # there were both a dollar amount and a real formula ("the price is $100, and the formula
    # $x^2$ nearby"), the first "$" of the sum was mistakenly paired with the first "$" of the formula —
    # the result was WORSE than the original: truncated amounts plus an orphaned “$” at the end
    # ("price 100, and the formula x²$ is nearby"). A tight "$...$" pair is stripped only
    # around the formula; numeric content remains currency.
    assert lumen_formatting._scrub_latex("цена $100, а формула $x^2$ рядом") == "цена $100, а формула x² рядом"
    assert lumen_formatting._scrub_latex("первый вариант — $50, второй — $100") == "первый вариант — $50, второй — $100"
    assert lumen_formatting._scrub_latex("стоимость: $100. Итого: $200.") == "стоимость: $100. Итого: $200."
    assert lumen_formatting._scrub_latex("$100/$50") == "$100/$50"
    assert lumen_formatting._scrub_latex("$100–$50") == "$100–$50"
    assert lumen_formatting._scrub_latex(r"\$100\$ и $x$") == r"\$100\$ и x"


def test_scrub_latex_plain_fallback_renders_pythagoras_without_raw_markup():
    # Prod 23.11.2026: a rich message did not render on the client, and the final showed
    # "$c$", "text{cm}", and "^2". The HTML/plain fallback must remain readable.
    source = (
        "Формула $c^2 = a^2 + b^2$, площадь "
        r"$S = \frac{ab}{2} = 6\text{ см}^2$ и матрица $\begin{matrix}a & b\end{matrix}$; цена $100."
    )
    result = lumen_formatting._scrub_latex(source)
    assert result == "Формула c² = a² + b², площадь S = ab/2 = 6 см² и матрица a b; цена $100."


def test_strip_markdown_scrubs_math_but_protects_code_and_urls():
    source = r"Формула $x^2$, код `C:\tmp\$x$` и ссылка https://example.com/$x$"
    result = lumen_formatting._strip_markdown(source)
    assert result == r"Формула x², код C:\tmp\$x$ и ссылка https://example.com/$x$"



def test_rich_math_keeps_currency_and_escaped_dollars():
    assert lumen_formatting._md_to_rich_html("$100/$50") == "$100/$50"
    assert lumen_formatting._md_to_rich_html("$100–$50") == "$100–$50"
    assert lumen_formatting._md_to_rich_html(r"\$100\$ и $x$") == r"\$100\$ и <tg-math>x</tg-math>"


def test_bare_url_with_dollars_survives_html_and_rich_scrub():
    url = "https://example.com/$x$?a=1&b=2"
    assert lumen_formatting._md_to_html(url) == "https://example.com/$x$?a=1&amp;b=2"
    assert lumen_formatting._md_to_rich_html(url) == "https://example.com/$x$?a=1&amp;b=2"


def test_bare_url_does_not_eat_markdown_emphasis():
    url = "https://example.com"
    assert lumen_formatting._md_to_html(f"**{url}**") == f"<b>{url}</b>"
    assert lumen_formatting._md_to_rich_html(f"**{url}**") == f"<b>{url}</b>"
    assert lumen_formatting._md_to_html(f"*{url}*") == f"<i>{url}</i>"
    assert lumen_formatting._md_to_rich_html(f"~~{url}~~") == f"<s>{url}</s>"
    assert lumen_formatting._strip_markdown(f"**{url}**") == url


@pytest.mark.parametrize(("source", "html_expected", "plain_expected"), [
    ("**https://example.com**.", "<b>https://example.com</b>.", "https://example.com."),
    ("*https://example.com*.", "<i>https://example.com</i>.", "https://example.com."),
    ("~~https://example.com~~;", "<s>https://example.com</s>;", "https://example.com;"),
    ("**https://example.com**…", "<b>https://example.com</b>…", "https://example.com…"),
    ("**https://example.com**»", "<b>https://example.com</b>»", "https://example.com»"),
    ('"**https://example.com**.', '"<b>https://example.com</b>.', '"https://example.com.'),
])
def test_bare_url_keeps_emphasis_before_sentence_punctuation(source, html_expected, plain_expected):
    assert lumen_formatting._md_to_html(source) == html_expected
    assert lumen_formatting._md_to_rich_html(source) == html_expected
    assert lumen_formatting._strip_markdown(source) == plain_expected


def test_inline_math_has_no_arbitrary_length_limit():
    formula = "x" * 250
    source = f"${formula}$"
    assert lumen_formatting._scrub_latex(source) == formula
    assert lumen_formatting._md_to_rich_html(source) == f"<tg-math>{formula}</tg-math>"


@pytest.mark.parametrize(("source", "plain", "rich"), [
    ("$-$", "-", "<tg-math>-</tg-math>"),
    ("$x-$", "x-", "<tg-math>x-</tg-math>"),
    ("$x$2", "x2", "<tg-math>x</tg-math>2"),
])
def test_currency_heuristic_does_not_hide_real_inline_math(source, plain, rich):
    assert lumen_formatting._scrub_latex(source) == plain
    assert lumen_formatting._md_to_rich_html(source) == rich


def test_inline_math_asterisks_are_not_markdown_emphasis():
    assert lumen_formatting._md_to_html("$2*3*4$") == "2∗3∗4"
    assert lumen_formatting._strip_markdown("$2*3*4$") == "2∗3∗4"


def test_scrub_latex_order_sensitive_replacements_dont_corrupt_each_other():
    # FUTURE REGRESSION GUARD: _LATEX_SYMBOL_MAP uses plain str.replace() in order
    # dictionary insertion order, not regex. "le" is a substring of "leq", "in" is a substring
    # "\infty" ("\in" + "fty"). If the dictionary order is ever changed so
    # that a short command comes before a longer one starting with the same
    # substring, the result would be corrupted ("∈fty" instead of "∞", etc.). Here is the fix
    # the EXACT order (leq/geq/neq/infty before le/ge/ne/in) — the test checks
    # final behavior, not the dictionary order itself, so it will survive refactoring,
    # if it preserves correctness.
    assert lumen_formatting._scrub_latex(r"a \leq b \le c") == "a ≤ b ≤ c"
    assert lumen_formatting._scrub_latex(r"a \geq b \ge c") == "a ≥ b ≥ c"
    assert lumen_formatting._scrub_latex(r"a \neq b \ne c") == "a ≠ b ≠ c"
    assert lumen_formatting._scrub_latex(r"x \in S, \infty") == "x ∈ S, ∞"


def test_scrub_latex_protected_inside_code_blocks_via_full_pipeline():
    # The full _md_to_html pipeline extracts code BEFORE calling _scrub_latex — backticks
    # backslashes in real code (regexes, Windows paths) must not be affected.
    text = "```python\nimport re\npattern = re.compile(r\"\\d+\")\n```\nформула \\(\\pi r^2\\) вне кода."
    result = lumen_formatting._md_to_html(text)
    assert "\\d+" in result
    assert "π r²" in result


# ─────────────────── normalize list markers "- "/"* " → "• " ───────────────────
# Regression for a real space found: _md_to_html converts **bold**/*italic*/
# `code`/tables, but previously did NOT touch ordinary markdown lists — they were sent to
# Telegram literally with "-"/"*" at the start of a line.

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
    # A table delimiter row ("---|---") must not be mistaken for
    # a list marker — it has no space immediately after the first hyphen.
    text = "Название | Цена\n---|---\nКофе | 150"
    assert lumen_formatting._normalize_bullet_markers(text) == text


def test_split_inline_bullets_splits_long_run_into_lines():
    # Prod 22.09.2026: the model wrote an entire comparison list in one paragraph using "•".
    items = ["пункт %d с развёрнутым текстом для набора длины абзаца" % i for i in range(5)]
    text = "Вот моменты сравнения характеристик: " + " • ".join(items)
    assert len(text) >= 200
    result = lumen_formatting._split_inline_bullets(text)
    lines = result.split("\n")
    assert lines[0] == "Вот моменты сравнения характеристик: " + items[0]
    assert lines[1:] == ["• " + item for item in items[1:]]


# ─────────────────── I25: HTML/Rich parity and data loss ───────────────────

def test_escaped_pipe_stays_inside_table_cell_in_html_and_rich():
    # R5-03 (26.11.2026 audit): an escaped pipe is cell content,
    # not a separator; otherwise the next value disappears during Rich rendering.
    source = "| Значение | Следующее |\n| --- | --- |\n| a \\| b | c |"
    assert lumen_formatting._split_table_cells(r"| a \| b | c |") == ["a | b", "c"]
    html = lumen_formatting._md_to_html(source)
    rich = lumen_formatting._md_to_rich_html(source)
    assert "<b>Значение:</b> a | b; <b>Следующее:</b> c" in html
    assert "<th>Значение</th><th>Следующее</th>" in rich
    assert "<td>a | b</td><td>c</td>" in rich


def test_latex_symbol_replacement_respects_command_boundary():
    # R5-04: \le must not consume the prefix of the unknown command \left.
    source = r"\left(x\right) и \le q"
    assert lumen_formatting._scrub_latex(source) == "left(xright) и ≤ q"
    assert lumen_formatting._md_to_html(source) == "left(xright) и ≤ q"
    assert lumen_formatting._md_to_rich_html(source) == "left(xright) и ≤ q"


@pytest.mark.parametrize("render", [lumen_formatting._md_to_html, lumen_formatting._md_to_rich_html])
def test_code_fence_accepts_common_info_string_tokens(render):
    # R5-08: c++/c#/objective-c are ordinary info strings, not raw text.
    for lang in ("c++", "c#", "objective-c"):
        result = render(f"```{lang}\nint x = 1;\n```")
        assert f'class="language-{lang}"' in result
        assert "int x = 1;" in result
        assert "```" not in result


@pytest.mark.parametrize("render", [lumen_formatting._md_to_html, lumen_formatting._md_to_rich_html])
def test_single_hyphen_table_separator_is_recognized(render):
    # R5-09: GFM permits a single hyphen in a delimiter cell.
    source = "| Имя | Цена |\n| - | - |\n| Чай | 100 |"
    result = render(source)
    assert "| - | - |" not in result
    assert "Чай" in result and "100" in result


def test_escaped_emphasis_is_literal_in_all_render_paths():
    # R5-10: escaped asterisks/underscores do not become tags.
    source = r"\*literal\* и \_literal\_"
    assert lumen_formatting._md_to_html(source) == "*literal* и _literal_"
    assert lumen_formatting._md_to_rich_html(source) == "*literal* и _literal_"
    assert lumen_formatting._strip_markdown(source) == "*literal* и _literal_"


def test_rich_math_block_extracted_before_inline_math():
    # R5-11: block \[...\] is extracted before $...$, otherwise the placeholder
    # inserted inline math remains inside an already completed tg-math-block.
    source = r"\[ a $x$ b \]"
    result = lumen_formatting._md_to_rich_html(source)
    assert result == "<tg-math-block> a $x$ b </tg-math-block>"
    assert "\x00" not in result


@pytest.mark.parametrize(("source", "expected"), [
    (r"\[ a \* b \]", "<tg-math-block> a * b </tg-math-block>"),
    (r"$a \* b$", "<tg-math>a * b</tg-math>"),
    (r"# h \*x\*", "<h2>h *x*</h2>"),
])
def test_escaped_emphasis_survives_rich_structural_wrappers(source, expected):
    # Review I25 round 2: the EM placeholder is restored after math/heading/table.
    assert lumen_formatting._md_to_rich_html(source) == expected


def test_escaped_emphasis_in_rich_table_cell_has_no_placeholder():
    source = "| a | b |\n| --- | --- |\n| \\*x\\* | c |"
    result = lumen_formatting._md_to_rich_html(source)
    assert "<td>*x*</td><td>c</td>" in result
    assert "\x00" not in result


def test_even_backslashes_do_not_escape_emphasis_marker():
    source = r"\\*x*"
    assert "<i>x</i>" in lumen_formatting._md_to_html(source)
    assert "<i>x</i>" in lumen_formatting._md_to_rich_html(source)
    assert "\x00" not in lumen_formatting._md_to_html(source)


def test_escaped_emphasis_placeholder_cannot_collide_with_source():
    source = r"\*x\*" + "\x00EM0\x00"
    expected = "*x*\x00EM0\x00"
    assert lumen_formatting._md_to_html(source) == expected
    assert lumen_formatting._md_to_rich_html(source) == expected
    assert lumen_formatting._strip_markdown(source) == expected


@pytest.mark.parametrize(("source", "expected"), [
    ("`code`" + r"\*x\*", "<code>code</code>*x*"),
    ("[link](https://example.com)" + r"\_x\_", '<a href="https://example.com">link</a>_x_'),
])
@pytest.mark.parametrize("render", [lumen_formatting._md_to_html, lumen_formatting._md_to_rich_html])
def test_escaped_emphasis_after_placeholder_is_restored_exactly(render, source, expected):
    # Review I25 round 3: the shared NUL of an adjacent placeholder does not belong to the EM key.
    assert render(source) == expected


@pytest.mark.parametrize(("source", "expected"), [
    ("`" + "\x00EM0\x00" + "`" + r"\*x\*", "\x00EM0\x00*x*"),
    ("https://example.com/" + "\x00EM0\x00" + " " + r"\_x\_",
     "https://example.com/\x00EM0\x00 _x_"),
])
def test_plain_fallback_does_not_rewrite_marker_inside_code_or_url(source, expected):
    # Review I25 round 4: the original code/URL text is restored after EM cleaning.
    assert lumen_formatting._strip_markdown(source) == expected


@pytest.mark.parametrize("render", [lumen_formatting._md_to_html, lumen_formatting._md_to_rich_html])
def test_escaped_emphasis_inside_link_label_is_literal(render):
    source = r"[\*x\*](https://example.com)"
    assert render(source) == '<a href="https://example.com">*x*</a>'
    assert lumen_formatting._strip_markdown(source) == "*x*"


@pytest.mark.parametrize("index", ["0", "999"])
@pytest.mark.parametrize("render", [
    lumen_formatting._md_to_html,
    lumen_formatting._md_to_rich_html,
    lumen_formatting._strip_markdown,
])
def test_latex_cleanup_cannot_create_emphasis_placeholder(render, index):
    # I25 review, round 5: removing 	ext{} joins hex tokens. The service
    # the marker must be absent after such transformations as well.
    prefix = "0123456789abcdef"
    source = prefix + r"0\text{}0EM" + index + "\x00" + r"\*x\*"
    assert render(source) == prefix + "00EM" + index + "\x00*x*"


@pytest.mark.parametrize("index", ["0", "999"])
@pytest.mark.parametrize("render", [
    lumen_formatting._md_to_html,
    lumen_formatting._md_to_rich_html,
    lumen_formatting._strip_markdown,
])
def test_latex_cleanup_cannot_create_private_use_marker(render, index):
    # Review I25 round 6: deleting \text{} can join existing private-use
    # characters, so absence of a multi-character token is not enough.
    prefix = "".join(chr(code) for code in range(0xE000, 0xE040))
    source = prefix + "\ue000\\text{}\ue000EM" + index + "\x00" + r"\*x\*"
    assert render(source) == prefix + "\ue000\ue000EM" + index + "\x00*x*"


def test_escaped_emphasis_protection_is_linear_on_large_input():
    source = r"\*" * 8000
    started = time.monotonic()
    assert lumen_formatting._md_to_html(source) == "*" * 8000
    assert time.monotonic() - started < 1.0


def test_escaped_emphasis_marker_selection_is_linear_on_long_nul_run():
    prefix = "\x00" * 32000 + "EM0\x00"
    source = prefix + r"\*x\*"
    started = time.monotonic()
    assert lumen_formatting._strip_markdown(source) == prefix + "*x*"
    assert time.monotonic() - started < 0.5


def test_escaped_emphasis_long_nul_prefix_does_not_inflate_every_key():
    prefix = "\x00" * 4000
    source = prefix + r"\*" * 4000
    protected, _saved = lumen_formatting._protect_escaped_emphasis(source)
    assert len(protected) <= len(source) + 8 * 4000
    assert lumen_formatting._strip_markdown(source) == prefix + "*" * 4000


def test_orphan_table_rows_respect_escaped_pipes():
    source = "| a \\| b | c |\n| d \\| e | f |"
    assert lumen_formatting._convert_orphan_table_rows(source) == "• a | b — c\n• d | e — f"


def test_single_hyphen_with_mismatched_header_is_not_a_table():
    source = "foo | bar\n-\nbaz | qux"
    assert lumen_formatting._is_table_separator("-") is False
    assert lumen_formatting._convert_markdown_tables_to_lists(source) == source
    assert "<table" not in lumen_formatting._md_to_rich_html(source)



def test_split_inline_bullets_ignores_short_prose():
    # Short "tea • coffee" and two separators are ordinary prose, not a list.
    assert lumen_formatting._split_inline_bullets("На выбор чай • кофе • сок.") == "На выбор чай • кофе • сок."
    assert lumen_formatting._split_inline_bullets("Плюсы • минусы") == "Плюсы • минусы"
    assert lumen_formatting._split_inline_bullets("На выбор чай • кофе.") == "На выбор чай • кофе."


@pytest.mark.parametrize(("text", "n_lines", "intro"), [
    # Prod 26.09.2026: exactly 3 items (2 separators) — the most common real case.
    (
        "• Рассеяние Рэлея: мелкие частицы в атмосфере рассеивают коротковолновый свет. "
        "• Солнечный спектр: солнце излучает больше синего света днём и ночью. "
        "• Отсутствие поглощения: газы атмосферы почти не поглощают синий свет.",
        3, None,
    ),
    # Production 05.10.2026: concatenation had no spaces around "•" at all.
    (
        "• Отвечать на вопросы, вести беседу.• Выполнять веб-поиск и использовать полученные данные. "
        "•Читать открытые веб-страницы и анализировать их содержание.• Анализировать присланные фото, видео и документы.",
        4, None,
    ),
    # Same concatenation with the "·" marker: a different character, same blob for the reader.
    (
        "Итоги такие: первый пункт с подробностями для длины строки и смыслом · второй пункт с подробностями "
        "для длины строки и смыслом · третий пункт с подробностями для длины строки и смыслом · четвёртый пункт.",
        4, "Итоги такие: ",
    ),
    # Prod 07.10.2026: platform rows (~150 characters) were not caught by the 200 threshold.
    (
        "Telegram: • Поищи канал по названию и нажми вступить для проверки длины строки • Часто приватные каналы шлют приглашения в личку",
        3, "Telegram:",
    ),
])
def test_split_inline_bullets_splits_prod_answers(text, n_lines, intro):
    assert len(text) >= 120
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
    # Prod 25.09.2026: the model wrote "1. ... 2. ... 3. ..." in a single paragraph.
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
    # Two items without a third, years/versions, and a reference to "items 1 and 2" are not lists.
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
    # The Rich path previously did not split run-together text at all (prod 25.09.2026: melodramas and the verdict on one line).
    bullets = "Вот несколько хороших мелодрамм: " + " • ".join(
        ["фильм номер %d с тёплым и подробным описанием сюжета" % i for i in range(4)]
    )
    assert "<br/>• " in lumen_formatting._md_to_rich_html(bullets)
    numbered = "1. Пункт первый с достаточным пояснением для проверки. 2. Пункт второй с достаточным пояснением для проверки. 3. Пункт третий с достаточным пояснением для проверки."
    rich = lumen_formatting._md_to_rich_html(numbered)
    assert "<br/>2. " in rich and "<br/>3. " in rich


def test_md_to_rich_html_scrubs_stray_latex_like_the_plain_path():
    # Regression (audit 26.09.2026): the rich path left raw LaTeX outside separators
    # ("rac", "lpha	imes"), while the regular one converted it to Unicode — the same answer
    # looked different in the stream and in the final output.
    b = chr(92)
    html = lumen_formatting._md_to_html(f"S = {b}frac{{1}}{{2}}{b}pi r^2 and {b}alpha{b}times{b}beta")
    rich = lumen_formatting._md_to_rich_html(f"S = {b}frac{{1}}{{2}}{b}pi r^2 and {b}alpha{b}times{b}beta")
    assert html == rich
    assert b not in rich, "сырые слэши в финальном сообщении — провал промта"
    assert "1/2" in rich and "α" in rich


def test_md_to_rich_html_keeps_delimited_math_untouched():
    # $...$ goes into <tg-math> as is (the only intentional difference between rich
    # from the normal path, where the same formula becomes Unicode).
    rich = lumen_formatting._md_to_rich_html("скорость $x^2$ тут")
    assert "<tg-math>x^2</tg-math>" in rich


def test_render_paths_parity_on_shared_cases():
    # Parametrized parity test for two rendering paths (regular HTML and rich):
    # differ only in headings ($x$ → <tg-math>); everything else must
    # match, otherwise the stream and final message show different things.
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
        # A newline in rich text is <br/> (prod 07.10.2026: a bare "\n"
        # was joined together); in regular HTML it is "\n". Compare after normalization.
        rich = lumen_formatting._md_to_rich_html(case).replace("<br/>", "\n")
        assert lumen_formatting._md_to_html(case) == rich, case


def test_render_paths_differ_only_for_headings():
    # Headings are the only intentional difference: rich supports <h3>, while regular
    # path emits <b>. Verify that this is the only thing that diverges.
    assert lumen_formatting._md_to_html("## Title") == "<b>Title</b>"
    assert lumen_formatting._md_to_rich_html("## Title") == "<h3>Title</h3>"


def test_md_to_rich_html_turns_newlines_into_br():
    # Prod 07.10.2026: in the final rich message, a split list arrived as one
    # mash because Telegram joins paragraphs and a bare "\n" is not a break.
    # Rich produces only <br/> (the docs also show <blockquote>). The old code
    # kept "\n", so the list looked merged.
    b = chr(96) * 3
    text = "список:\n• раз\n• два\n\n" + b + "python\nx = 1\ny = 2\n" + b
    rich = lumen_formatting._md_to_rich_html(text)
    assert "список:<br/>• раз<br/>• два<br/><br/>" in rich
    # Newlines inside a code block must remain newlines.
    assert '<pre><code class="language-python">x = 1\ny = 2</code></pre>' in rich
    # Outside <pre>, no bare newlines remain.
    assert "\n" not in rich.replace("x = 1\ny = 2", "")


def test_md_to_rich_html_splits_three_bullets_prod_movies():
    # Prod 26.09.2026: in an answer about melodramas, 3 items merged, while the
    # rich path stayed silent.
    text = (
        "• Амелі (2001): французская камедыя-фэнтэзі пра маладых, якія бачаць свет у яркіх фарбах. "
        "• Падынгтан (2014): лёгкі сямейны фільм пра мілога мядзведзя, які шукае новы дом у Лондане. "
        "• Crazy Rich Asians (2018): вясёлая рамантычная камедыя пра кітайскую эліту, поўная колеру і музыкі."
    )
    assert len(text) >= 200
    assert "<br/>• " in lumen_formatting._md_to_rich_html(text)


def test_md_to_html_full_pipeline_converts_bullet_list_with_bold():
    text = "* **Возмездие:** аргумент про справедливость\n* **Сдерживание:** снижает преступность"
    result = lumen_formatting._md_to_html(text)
    assert result.startswith("• <b>Возмездие:</b>")
    assert "\n• <b>Сдерживание:</b>" in result
    assert "*" not in result.replace("</b>", "").replace("<b>", "")


# ──────────────── markdown headings "#"/"##"/"###" → **bold text** ────────────────
# REGRESSION found in real Telegram screenshots (August 18, 2026):
# system_prompt.py forbids markdown headings, but models (especially free
# OpenRouter models) regularly write them anyway — previously "###" was sent to
# Telegram literally, with no protective grid at all (unlike tables/lists).

@pytest.mark.parametrize(("source", "expected"), [
    ("### Как она выводится?", "**Как она выводится?**"),
    ("# Заголовок", "**Заголовок**"),
    ("## Подзаголовок", "**Подзаголовок**"),
])
def test_normalize_headers_converts_atx_to_bold(source, expected):
    assert lumen_formatting._normalize_headers(source) == expected


def test_normalize_headers_does_not_touch_hash_mid_line():
    # "C#" / "#tag" not at the start of a line: an ATX heading ONLY at the start of a line.
    text = "Язык C# отличается от C++.\nПодробнее: #tag"
    assert lumen_formatting._normalize_headers(text) == text


def test_normalize_headers_requires_space_after_hashes():
    # '#no_space' is not an ATX heading under CommonMark rules; leave it alone.
    assert lumen_formatting._normalize_headers("#без_пробела текст") == "#без_пробела текст"


def test_normalize_headers_bare_hashes_with_no_text_removed():
    assert lumen_formatting._normalize_headers("### \nследующая строка") == "\nследующая строка"


def test_normalize_headers_does_not_double_wrap_already_bold_content():
    # "### **Important**" means the model already wrapped the text in bold; wrapping it again
    # would produce "****Important****" and break "**" pairing in Phase 3.
    assert lumen_formatting._normalize_headers("### **Важно**") == "**Важно**"


def test_md_to_html_full_pipeline_strips_stray_header_markers():
    # Regression for a real bug found: the user saw literal “###” in
    # in a bot message instead of bold text; the full pipeline must fix this.
    result = lumen_formatting._md_to_html("### Как она выводится?\nобычный текст")
    assert "###" not in result
    assert result.startswith("<b>Как она выводится?</b>")


def test_md_to_html_header_hash_inside_code_block_untouched():
    # "#" inside a code block (for example, a Python comment) must not count
    # heading—the code was already moved by a placeholder before this phase.
    text = "```python\n# обычный комментарий\nprint(1)\n```"
    result = lumen_formatting._md_to_html(text)
    assert "# обычный комментарий" in result
    assert "<b>" not in result


# ─────────────────── spoiler tag: safety net (same principle as <u>) ───────────────────

@pytest.mark.parametrize(("source", "expected"), [
    ("<tg-spoiler>секрет</tg-spoiler>", "секрет"),
    ('<span class="tg-spoiler">секрет</span>', "секрет"),
])
def test_md_to_html_strips_literal_spoiler_tags(source, expected):
    assert lumen_formatting._md_to_html(source) == expected


# ─────────────────── syntax highlighting: the language from ```fence is preserved ───────────────────
# (the language-specific case is covered by test_md_to_html_code_block above)

def test_md_to_html_code_block_without_language_unchanged():
    assert lumen_formatting._md_to_html("```\nprint(1)\n```") == "<pre>print(1)</pre>"


# markdown quotes "> " -> <blockquote>

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
    # "5 > 3" is an ordinary comparison, not at the start of a line — it must not become a quote.
    assert lumen_formatting._md_to_html("сравнение: 5 > 3") == "сравнение: 5 &gt; 3"


# ─────────────────── markdown links [text](url) → <a href="url">text</a> ───────────────────

def test_md_to_html_converts_markdown_link():
    result = lumen_formatting._md_to_html("[почитать здесь](https://example.com/page)")
    assert result == '<a href="https://example.com/page">почитать здесь</a>'


def test_md_to_html_replaces_mismatched_url_label():
    # Audit 28.10.2026: a label URL pointing to another host is ready-made phishing.
    html = lumen_formatting._md_to_html("[https://bank.example](https://attacker.example/x)")
    assert ">https://attacker.example/x<" in html
    assert "bank.example" not in html
    # An ordinary text marker is not touched.
    assert ">Reuters<" in lumen_formatting._md_to_html("[Reuters](https://reuters.com/x)")


def test_md_to_html_link_with_underscores_in_url_not_corrupted_by_italic():
    # Regression: a URL with two "_" could be mistaken for a pair of italic
    # markers if link conversion ran before bold/italic in Phase 3.
    result = lumen_formatting._md_to_html("[текст](https://example.com/foo_bar_baz)")
    assert result == '<a href="https://example.com/foo_bar_baz">текст</a>'
    assert "<i>" not in result


def test_md_to_html_link_with_parenthesized_url_survives():
    # Review 15.11.2026: Wikipedia URLs contain parentheses ("Doom_(1993_video_game)").
    # The old regex `[^\s()]+` rejected them, leaving raw text and letting italic parsing add
    # <i> inside. Parentheses at one pair's level are allowed in URLs without swallowing the
    # closing Markdown link bracket.
    result = lumen_formatting._md_to_html("[Doom](https://en.wikipedia.org/wiki/Doom_(1993_video_game))")
    assert result == '<a href="https://en.wikipedia.org/wiki/Doom_(1993_video_game)">Doom</a>'
    result = lumen_formatting._md_to_html("[TOP](https://minecraft.fandom.com/wiki/The_One_Probe_(mod))")
    assert 'href="https://minecraft.fandom.com/wiki/The_One_Probe_(mod)"' in result
    assert "<i>" not in result


def test_md_to_html_link_text_markdown_not_processed_intentionally():
    # Link text is removed by a placeholder in Phase 1 (see the code comment) —
    # markdown inside it is intentionally unsupported (it was not requested); the output is
    # like ordinary escaped text, rather than becoming garbled or turning into <b>.
    result = lumen_formatting._md_to_html("[**жирная ссылка**](https://example.com)")
    assert result == '<a href="https://example.com">**жирная ссылка**</a>'


def test_md_to_html_non_http_bracket_text_left_alone():
    # "[note]" without an http(s) link is not a Markdown link and must not become <a>.
    assert lumen_formatting._md_to_html("текст [note] продолжение") == "текст [note] продолжение"


def test_md_to_html_link_url_with_quote_is_escaped():
    result = lumen_formatting._md_to_html('[текст](https://example.com/"injected)')
    assert '&quot;' in result
    assert '"injected' not in result


# ─────────────────── _md_to_rich_html (Bot API 10.1+, sendRichMessage) ───────────────────
# The same pipeline as _md_to_html, but tables/headings/LaTeX use real
# rich tags — Telegram's server renders them itself. The tests pin the contract,
# on which rich sending in bot.py depends.

def test_rich_table_renders_bordered_table():
    result = lumen_formatting._md_to_rich_html("| A | B |\n|---|---|\n| 1 | 2 |")
    assert result == "<table bordered><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"


def test_bare_url_does_not_consume_table_cell_separator():
    source = "| A | B |\n|---|---|\n| https://example.com|значение |"
    assert lumen_formatting._md_to_rich_html(source) == (
        "<table bordered><tr><th>A</th><th>B</th></tr>"
        "<tr><td>https://example.com</td><td>значение</td></tr></table>"
    )
    html = lumen_formatting._md_to_html(source)
    assert "https://example.com" in html
    assert "значение" in html
    assert "|" not in html
def test_rich_wide_table_splits_into_narrow_blocks():
    # Prod 24.11.2026: a comparison of four services ran past the right edge
    # Telegram Desktop. Repeat the first column so the chunks remain
    # meaningful, and do not show more than three columns at a time.
    result = lumen_formatting._md_to_rich_html(
        "| Критерий | Obsidian | Notion | Apple Notes |\n"
        "|---|---|---|---|\n"
        "| Связи | backlinks | базы | заметки |"
    )
    assert result.count("<table bordered compact>") == 2
    assert result.count("<th>Критерий</th>") == 2
    assert "<th>Obsidian</th>" in result and "<th>Notion</th>" in result
    assert "<th>Apple Notes</th>" in result
    assert result.count("<td>Связи</td>") == 2
    assert result.count("<td>backlinks</td>") == 1
    assert result.count("<td>базы</td>") == 1
    assert result.count("<td>заметки</td>") == 1
    assert all(table.count("<th>") <= 3 for table in result.split("</table>")[:-1])


def test_rich_wide_table_keeps_one_block_when_split_would_break_limits():
    # Repeating the first column must not hit the 4096 or 500-block limit:
    # keep one compact block for large tables, otherwise the rich path is lost.
    rows = "\n".join(f"| row{i} | a{i} | b{i} | c{i} |" for i in range(60))
    many_rows = lumen_formatting._md_to_rich_html(
        "| key | a | b | c |\n|---|---|---|---|\n" + rows
    )
    assert many_rows.count("<table bordered compact>") == 1
    assert many_rows.count("<tr>") == 61

    header = "| key |" + "".join(f" c{i} |" for i in range(19))
    separator = "|" + "---|" * 20
    wide_rows = "\n".join(
        "| row" + str(i) + " |" + "".join(f" v{i}_{j} |" for j in range(19)) + " |"
        for i in range(50)
    )
    many_columns = lumen_formatting._md_to_rich_html(header + "\n" + separator + "\n" + wide_rows)
    assert many_columns.count("<table bordered compact>") == 1
    assert many_columns.count("<th>") == 20
    assert many_columns.count("<tr>") == 51


def test_rich_table_cells_support_inline_markup_and_escape():
    result = lumen_formatting._md_to_rich_html("| H | K |\n|---|---|\n| **b** <x> | 2 |")
    assert result == "<table bordered><tr><th>H</th><th>K</th></tr><tr><td><b>b</b> &lt;x&gt;</td><td>2</td></tr></table>"


def test_rich_non_table_pipes_left_alone():
    # Single “|” without a separator row — not a table, leave the text as is.
    assert lumen_formatting._md_to_rich_html("a | b") == "a | b"


@pytest.mark.parametrize(("source", "expected"), [
    ("## Заголовок", "<h3>Заголовок</h3>"),
    ("### Подзаголовок", "<h4>Подзаголовок</h4>"),
    # Same tolerance as legacy normalization: leading spaces and 1–6 hash marks.
    ("   ## Заголовок", "<h3>Заголовок</h3>"),
    ("# Топ", "<h2>Топ</h2>"),
    ("#### Глубокий", "<h4>Глубокий</h4>"),
])
def test_rich_headings_become_h_tags(source, expected):
    assert lumen_formatting._md_to_rich_html(source) == expected


@pytest.mark.parametrize(("source", "expected"), [
    ("корень $x^2$ тут", "корень <tg-math>x^2</tg-math> тут"),
    ("формула $$E=mc^2$$ конец", "формула <tg-math-block>E=mc^2</tg-math-block> конец"),
    # Parenthesized LaTeX forms (a real nemotron case) are extracted before $.
    ("смотри \\[S = \\pi r^{2}\\] конец", "смотри <tg-math-block>S = \\pi r^{2}</tg-math-block> конец"),
    ("значение \\(x\\) тут", "значение <tg-math>x</tg-math> тут"),
])
def test_rich_math_preserved_as_tg_math_tags(source, expected):
    # Unlike _md_to_html (Unicode substitution), the rich path returns raw LaTeX —
    # Telegram's server renders it.
    assert lumen_formatting._md_to_rich_html(source) == expected


def test_rich_keeps_bold_links_code_quotes_and_escape():
    assert lumen_formatting._md_to_rich_html("**b**") == "<b>b</b>"
    assert lumen_formatting._md_to_rich_html("[t](https://example.com/a_b)") == '<a href="https://example.com/a_b">t</a>'
    assert lumen_formatting._md_to_rich_html("`<x>`") == "<code>&lt;x&gt;</code>"
    assert lumen_formatting._md_to_rich_html("5 < 10") == "5 &lt; 10"
    assert lumen_formatting._md_to_rich_html("> цитата") == "<blockquote>цитата</blockquote>"


def test_rich_math_inside_code_stays_code():
    # LaTeX inside code is code, not a formula (extraction order: code before math).
    assert lumen_formatting._md_to_rich_html("`$x$`") == "<code>$x$</code>"


def test_br_tag_becomes_line_break_in_both_paths():
    # Production case 17.09.2026: the model writes "<br>" inside table cells — without
    # processing reaches escape and appears literally ("...burned.<br>Pro Max").
    assert lumen_formatting._md_to_html("a<br>b") == "a\nb"
    assert lumen_formatting._md_to_html("a<br/>b") == "a\nb"
    assert lumen_formatting._md_to_rich_html("| H | K |\n|---|---|\n| a<br>b | 2 |") == (
        "<table bordered><tr><th>H</th><th>K</th></tr><tr><td>a<br/>b</td><td>2</td></tr></table>"
    )
    # "<blockquote>" must not be swallowed by the strict <br> pattern.
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
    # "$50 to $100" is content with a space at the boundary, not a formula. A lone symbol
    # without a pair — is also text.
    assert lumen_formatting._md_to_rich_html("от $50 до $100") == "от $50 до $100"
    assert lumen_formatting._md_to_rich_html("всего $80 000") == "всего $80 000"
    assert lumen_formatting._md_to_rich_html("$x$") == "<tg-math>x</tg-math>"


def test_inline_splitters_leave_structural_lines_alone():
    # Regression (adversarial review 27.09.2026): splitting merged lists ate quotes,
    # headings and markdown table rows — the user saw an empty quote panel,
    # a protruding "##" and a broken table.
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

    # A table without outer pipes: the line does not start with `|`, but the splitter must
    # recognize the table block; otherwise the HTML path split it before detection, while rich did not.
    bare_table = "Name | Age\n---|---\n1. alpharazat elfin uvicorn 2. betakakt elfin uvicorn 3. gamakakt elfin uvicorn | four"
    html_bare = lumen_formatting._md_to_html(bare_table)
    assert "<b>Name:</b>" in html_bare and "---" not in html_bare, html_bare
    rich_bare = lumen_formatting._md_to_rich_html(bare_table)
    assert "<table" in rich_bare and "1. alpharazat elfin uvicorn 2. betakakt elfin uvicorn 3. gamakakt elfin uvicorn" in rich_bare

    # Bullets also stay out of service lines (the length must exceed the 200 threshold).
    long_items = " • ".join(f"пункт номер {i} с достаточно длинным описанием" for i in range(4))
    for structural in (f"> вводная {long_items}", f"## вводная {long_items}", f"| вводная {long_items} | конец |"):
        assert lumen_formatting._split_inline_bullets(structural) == structural, structural[:60]
    long_numbered = "1. alpharazat elfin uvicorn 2. betakakt elfin uvicorn 3. gamakakt elfin uvicorn"
    for structural in (f"> {long_numbered}", f"## {long_numbered}"):
        assert "\n" not in lumen_formatting._split_inline_numbered(structural), structural[:60]


def test_inline_splitters_still_split_prose():
    # Edit boundary: ordinary prose is still split.
    prose = "1. alpharazat elfin uvicorn 2. betakakt elfin uvicorn 3. gamakakt elfin uvicorn"
    assert lumen_formatting._split_inline_numbered(prose).count("\n") == 2
    assert lumen_formatting._split_inline_bullets(prose) == prose


def test_scrub_latex_int_is_not_eaten_by_in():
    # Regression (hostile review 27.09.2026): in the character map, \in came before
    # \int,
    # so "\int_0^1" turned into "∈t₀¹".
    assert lumen_formatting._scrub_latex(r"\int_0^1 x dx") == "∫₀¹ x dx"
    assert lumen_formatting._scrub_latex(r"a \in b") == "a ∈ b"
    assert lumen_formatting._scrub_latex(r"\infty") == "∞"


def test_rich_heading_keeps_scrubbed_latex():
    # Regression (hostile review 27.09.2026): the heading went into a placeholder
    # BEFORE
    # the shared _scrub_latex, so "lpha" remained raw in <h3>.
    out = lumen_formatting._md_to_rich_html("## Коэффициент \\alpha и \\int_0^1")
    assert "\\alpha" not in out and "\\int" not in out
    assert "α" in out and "∫" in out


def test_split_does_not_leave_a_heading_at_the_end_of_a_chunk():
    # Live response 11.11.2026: the bubble ended with the line "III. Results", while the text
    # of the section moved to the next one; with Claude, a heading is not left
    # hanging alone.
    import lumen_formatting as F
    chunks = [
        "Первый раздел кончается здесь.\n\nIII. Итоги",
        "Потери: около десяти миллионов погибших.",
    ]
    out = F._pull_dangling_headings(chunks, 4096)
    assert out[0].endswith("здесь.")
    assert out[1].startswith("III. Итоги")
    assert "Потери" in out[1]


def test_split_moves_bold_heading_and_keeps_limit():
    import lumen_formatting as F
    out = F._pull_dangling_headings(["Абзац. \n\n**Итоги**", "Текст итогов."], 4096)
    assert out[0] == "Абзац."
    assert out[1].startswith("**Итоги**")
    # A heading under the limit moves with the first paragraph: if the next chunk
    # already full, it is cut, but a bubble containing only a heading does not appear.
    tight = F._pull_dangling_headings(["Абзац.\n\n**Итоги**", "x" * 60], 64)
    assert tight[0] == "Абзац."
    assert any(part.startswith("**Итоги**") for part in tight[1:])
    assert all(len(part) <= 64 for part in tight), [len(p) for p in tight]


def test_split_text_chunks_never_ends_a_chunk_on_a_heading():
    import lumen_formatting as F
    body = ("Это длинный абзац про причины и ход событий, он занимает место. " * 6)
    text = body + "\n\n## Итоги\n\n" + body
    chunks = F._split_text_chunks(text, 260)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert not chunk.rstrip().endswith("## Итоги"), chunk[-60:]


def test_inline_markup_keeps_word_underscores_and_nests_triple():
    # External audit 16.11.2026: '_' inside a file name and URL was cut by italics,
    # while "***x***" produced crossed tags and lost the formatting of the entire chunk.
    from lumen_formatting import _md_to_html
    html = _md_to_html("файл my_file_name.txt и https://example.com/a_b_c")
    assert "my_file_name.txt" in html
    assert "https://example.com/a_b_c" in html
    assert _md_to_html("***важно***") == "<b><i>важно</i></b>"
    assert _md_to_html("This is __important__") == "This is <b>important</b>"
    assert _md_to_html("|a| = |b|") == "|a| = |b|"
    assert _md_to_html("| sort -u | uniq -c |") == "| sort -u | uniq -c |"
    assert _md_to_html("```") == "```"


def test_long_table_never_splits_into_raw_pipes():
    # External audit 16.11.2026: a 120-line table was cut into chunks, and
    # headerless tail chunks went to the user as raw "|" characters.
    from lumen_formatting import _md_to_html, _split_text_chunks
    rows = ["| Модель | Описание |", "|---|---|"] + [f"| M{i} | текст {i} |" for i in range(120)]
    chunks = _split_text_chunks(chr(10).join(rows), 3000)
    assert len(chunks) > 1
    for chunk in chunks:
        assert "|" not in _md_to_html(chunk)


def test_unclosed_code_fence_is_closed_in_final_html():
    # External audit 16.11.2026: a truncated answer with an unclosed ``` displayed
    # the user a literal fence, although the stream tail closed it.
    from lumen_formatting import _md_to_html
    html = _md_to_html("код:" + chr(10) + "```python" + chr(10) + "x = 1")
    assert "```" not in html
    assert "x = 1" in html


def test_bare_urls_are_not_touched_by_inline_markup():
    # Fix verification 16.11.2026: "__b__" in a URL path became bold.
    from lumen_formatting import _md_to_html, _md_to_rich_html
    url = "https://x.dev/a/__b__/c"
    assert url in _md_to_html("см. " + url)
    assert url in _md_to_rich_html("см. " + url)
    # Double-underscore bold still works, but not across a line break.
    assert _md_to_html("This is __important__") == "This is <b>important</b>"
    assert _md_to_html("Имя: __" + chr(10) + "Дата: __").count("<b>") == 0


def test_emphasis_glued_to_a_bare_url_still_works():
    # Verification of fixes 16.11.2026: link extraction consumed trailing ** and *.
    from lumen_formatting import _md_to_html, _md_to_rich_html
    assert _md_to_html("**см. https://x.com/a**") == "<b>см. https://x.com/a</b>"
    assert _md_to_html("*https://x.com/a*") == "<i>https://x.com/a</i>"
    assert "https://x.com/a" in _md_to_rich_html("**см. https://x.com/a**")


def test_underscores_inside_bold_and_pipe_after_url():
    # Final checker 16.11.2026: "__total_cost__" stopped being bold,
    # and a table-cell link without spaces consumed the "|" separator.
    from lumen_formatting import _md_to_html, _md_to_rich_html
    assert _md_to_html("__total_cost__") == "<b>total_cost</b>"
    table = "| a | b |" + chr(10) + "|---|---|" + chr(10) + "| https://x.com/a|z |"
    assert _md_to_rich_html(table).count("<td>") == 2
