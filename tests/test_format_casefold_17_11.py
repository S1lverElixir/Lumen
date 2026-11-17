"""Регрессии линейных заменителей форматирования (ревью 17.11.2026)."""
import lumen_formatting as lf


def test_tag_replacement_survives_case_folding_length_change():
    # lower() меняет длину на İ/ß: индексы из копии сдвигали теги.
    assert "İstanbul <b>bold</b> ok" in lf._md_to_html("İstanbul <b>bold</b> ok")
    assert "<code>rm -rf</code>" in lf._md_to_html("DİKKAT: <code>rm -rf</code>")


def test_delimited_replacement_keeps_old_semantics():
    sample = "\\(" + "a" + chr(10) + "b" + "\\)"
    assert lf._replace_delimited(sample, "\\(", "\\)", lambda s: "M",
                                 allow_newlines=False) == sample
    inline = "\\(x\\)"
    assert lf._replace_delimited(inline, "\\(", "\\)", lambda s: "M") == "M"
    empty = "\\[\\]"
    assert lf._replace_delimited(empty, "\\[", "\\]", lambda s: "M", min_len=1) == empty
