"""Pure Markdown-to-Telegram-HTML formatting without I/O or mutable state."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

_URL_LABEL_RE = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)

# Provider artifacts in answer text: safety classifier labels
# (Groq/Llama) and tool-call markup on free OR models. In a chat
# this looks like garbage instead of an answer (05.11.2026 backup corpus: 39 characters
# 'User Safety: safe' in full and raw <|tool_call_start|> at 167).
_SAFETY_LABEL_RE = re.compile(r"^[ \t]*(?:user|response|prompt) safety[ \t]*:.*$", re.IGNORECASE | re.MULTILINE)
_TOOL_CALL_SPAN_RE = re.compile(r"<\|[^|>]{0,40}\|>(?:.*?<\|[^|>]{0,40}\|>)?", re.DOTALL)


def _strip_provider_artifacts(text: str) -> str:
    """Text without provider service markers; becomes empty if there was no response."""
    if not text:
        return ""
    cleaned = _TOOL_CALL_SPAN_RE.sub("", text)
    cleaned = _SAFETY_LABEL_RE.sub("", cleaned)
    return cleaned.strip()


def _safe_link_label(label: str, url: str) -> str:
    """A URL label pointing to another host is ready-made phishing: show the real address. Ordinary text labels ("Reuters") are left alone (audit 28.10.2026)."""
    if not _URL_LABEL_RE.match(label.strip()):
        return label
    try:
        label_host = urlparse(label if "://" in label else "https://" + label).netloc.lower()
        url_host = urlparse(url).netloc.lower()
    except ValueError:
        return label
    if label_host and url_host and label_host != url_host:
        return url
    return label

_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
# The regex is quadratic on long whitespace tails: cut the string to match,
# a separator wider than the live-message cap does not occur.
_TABLE_SEP_MAX_LEN = 500


def _is_table_separator(line: str, header_line: str | None = None) -> bool:
    """GFM table delimiter row: at least two cells and a match with the header."""
    if "-" not in line:
        return False
    if _TABLE_SEP_RE.match(line[:_TABLE_SEP_MAX_LEN]) is None:
        return False
    cells = _split_table_cells(line)
    if len(cells) < 2:
        return False
    return header_line is None or len(cells) == len(_split_table_cells(header_line))

def _split_table_cells(line: str) -> list[str]:
    r"""GFM table cells: \\| is a literal pipe, not a separator. An even number of slashes before a pipe marks a real boundary (audit 26.11.2026)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        slashes = len(s[:-1]) - len(s[:-1].rstrip("\\"))
        if slashes % 2 == 0:
            s = s[:-1]
    cells: list[str] = []
    buf: list[str] = []
    slashes = 0
    for ch in s:
        if ch == "|":
            if slashes % 2:
                buf.pop()
                buf.append("|")
            else:
                cells.append("".join(buf).strip())
                buf = []
            slashes = 0
        else:
            buf.append(ch)
            slashes = slashes + 1 if ch == "\\" else 0
    cells.append("".join(buf).strip())
    return cells

def _convert_orphan_table_rows(text: str) -> str:
    """Headerless table rows (an answer split across several messages) become
    "• a — b"; otherwise the user saw raw "|" (external audit 16.11.2026).
    Leave single lines untouched: "| sort -u |" and "|a| = |b|" are not tables."""
    if "|" not in text:
        return text

    def _is_row(line: str) -> bool:
        s = line.strip()
        return s.startswith("|") and s.endswith("|") and line.count("|") >= 2

    lines = text.split(chr(10))
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _is_row(lines[i]):
            j = i
            while j < n and _is_row(lines[j]):
                j += 1
            if j - i >= 2:
                for line in lines[i:j]:
                    cells = _split_table_cells(line)
                    cells = [c for c in cells if c and not set(c) <= set("-: ")]
                    if len(cells) >= 2:
                        out.append("• " + " — ".join(cells))
                    else:
                        out.append(line)
                i = j
                continue
        out.append(lines[i])
        i += 1
    return chr(10).join(out)


def _convert_markdown_tables_to_lists(text: str) -> str:
    """Telegram does not render tables: expand a "heading + dashes + rows" block
    into "• Heading: value". The rich path sends a real <table>."""
    if "|" not in text or "-" not in text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if "|" in line and i + 1 < n and "-" in lines[i + 1] and _is_table_separator(lines[i + 1], line):
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

def _table_block_line_indexes(lines: list[str]) -> set[int]:
    """Line indices for Markdown tables (header + separator + body), under the same
    conditions as _convert_markdown_tables_to_lists above. List splitters must
    not touch these lines: in the regular path, tables are expanded AFTER list
    splitting, so a line without a leading `|` (a valid table without outer
    pipes) would otherwise be broken before table detection, making the stream
    and final version differ."""
    idx: set[int] = set()
    i, n = 0, len(lines)
    while i < n:
        if "|" in lines[i] and i + 1 < n and "-" in lines[i + 1] and _is_table_separator(lines[i + 1], lines[i]):
            if len(_split_table_cells(lines[i])) >= 2:
                j = i + 2
                data = 0
                while j < n and "|" in lines[j] and lines[j].strip():
                    data += 1
                    j += 1
                if data:
                    idx.update(range(i, j))
                    i = j
                    continue
        i += 1
    return idx

# ── Safety net against raw LaTeX ──────────────────────────────────────────
# A real case found during calibration: nemotron-3-nano-30b-a3b:free produced
# "\[ S = \pi r^{2}, \]" and "\(x^{2}+y^{2}=r^{2}\)" instead of Unicode in an answer about
# the area of a circle. The prompt allows LaTeX for the rich path (<tg-math>), but this is
# a second line of defense for the normal path: we do not rely solely on the model obeying,
# back it up with deterministic Unicode post-processing.
_LATEX_SUPERSCRIPT_MAP = {"0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "n": "ⁿ"}
_LATEX_SUBSCRIPT_MAP = {"0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉"}
_INLINE_MATH_DOLLAR_RE = re.compile(r"(?<![\\$])\$(?!\$)([^\s$][^$\n]*?)(?<![\\\s])\$(?!\$)")
_LATEX_TEXT_RE = re.compile(r"\\(?:text|mathrm|mathbf|operatorname)\{([^{}]*)\}")
_LATEX_ENV_RE = re.compile(r"\\(?:begin|end)\{[^{}]+\}")
_CURRENCY_MATH_RE = re.compile(r"\d[\d\s.,]*")
# Commands are replaced in one pass with a (?!letters) boundary; otherwise \le consumes
# an unknown \left prefix and turns the formula into ≤ft (audit 26.11.2026).
_LATEX_SYMBOL_MAP: dict[str, str] = {
    r"\times": "×", r"\cdot": "·", r"\approx": "≈", r"\infty": "∞",
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥", r"\neq": "≠", r"\ne": "≠",
    r"\rightarrow": "→", r"\Rightarrow": "⇒", r"\to": "→",
    r"\forall": "∀", r"\exists": "∃", r"\emptyset": "∅", r"\cup": "∪", r"\cap": "∩",
    r"\int": "∫",  # BEFORE \in: otherwise "\int" was consumed as "\in" and produced "∈t" (adversarial review 27.09.2026)
    r"\in": "∈",
    r"\pi": "π", r"\pm": "±", r"\mp": "∓", r"\sum": "∑", r"\prod": "∏",
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\Gamma": "Γ", r"\theta": "θ",
    r"\lambda": "λ", r"\mu": "μ", r"\sigma": "σ", r"\Sigma": "Σ", r"\delta": "δ", r"\Delta": "Δ",
    r"\phi": "φ", r"\omega": "ω", r"\Omega": "Ω",
}
_LATEX_SYMBOL_RE = re.compile(
    "(?:" + "|".join(re.escape(cmd) for cmd in sorted(_LATEX_SYMBOL_MAP, key=len, reverse=True))
    + r")(?![a-zA-Z])"
)


_CI_NEEDLE_RE: dict[str, re.Pattern] = {}


def _find_ci(text: str, needle: str, start: int) -> int:
    """Index without a text.lower() copy: lowercasing İ/ß changes the length, so
    positions from the copy shifted (adversarial pass 17.11.2026)."""
    pattern = _CI_NEEDLE_RE.get(needle)
    if pattern is None:
        pattern = re.compile(re.escape(needle), re.IGNORECASE)
        _CI_NEEDLE_RE[needle] = pattern
    match = pattern.search(text, start)
    return match.start() if match else -1


def _replace_delimited(
    text: str, open_delim: str, close_delim: str, wrap,
    *, allow_newlines: bool = True, min_len: int = 0,
) -> str:
    """Paired delimiter replacement in one pass: an unmatched opening previously made the regex scan the rest of the line from every position, O(n²) (review 17.11.2026)."""
    out: list[str] = []
    i = 0
    while True:
        start = text.find(open_delim, i)
        if start == -1:
            out.append(text[i:])
            return "".join(out)
        end = text.find(close_delim, start + len(open_delim))
        if end == -1:
            out.append(text[i:])
            return "".join(out)
        inner = text[start + len(open_delim):end]
        if len(inner) < min_len or (not allow_newlines and "\n" in inner):
            # The old regex without DOTALL/with +? did not handle this case: leave it as is.
            out.append(text[i:end + len(close_delim)])
            i = end + len(close_delim)
            continue
        out.append(text[i:start])
        out.append(wrap(inner))
        i = end + len(close_delim)


def _replace_tag_pairs(text: str, opens: tuple[str, ...], closes: tuple[str, ...], wrap) -> str:
    """The same pass for tags: a DOTALL regex on an unmatched <b> is O(n²)."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i <= n:
        best = None
        best_tag = None
        for tag in opens:
            pos = _find_ci(text, tag, i)
            if pos != -1 and (best is None or pos < best):
                best, best_tag = pos, tag
        if best is None:
            out.append(text[i:])
            break
        inner_start = best + len(best_tag)
        close_pos = None
        close_tag = None
        for tag in closes:
            pos = _find_ci(text, tag, inner_start)
            if pos != -1 and (close_pos is None or pos < close_pos):
                close_pos, close_tag = pos, tag
        if close_pos is None:
            out.append(text[i:])
            break
        out.append(text[i:best])
        out.append(wrap(text[inner_start:close_pos]))
        i = close_pos + len(close_tag)
    return "".join(out)


_SPOILER_SPAN_OPEN_RE = re.compile(r'<span\s+class=["\']tg-spoiler["\']>', re.IGNORECASE)


def _replace_spoiler_spans(text: str) -> str:
    """Linear pass for a span with a spoiler: the same failure class."""
    out: list[str] = []
    i = 0
    while True:
        m = _SPOILER_SPAN_OPEN_RE.search(text, i)
        if m is None:
            out.append(text[i:])
            return "".join(out)
        end = _find_ci(text, "</span>", m.end())
        if end == -1:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:m.start()])
        out.append(text[m.end():end])
        i = end + len("</span>")

def _absent_marker_char(text: str) -> str:
    """Return an absent private-use or unassigned character.

    Formatting transforms only delete source characters or insert fixed assigned
    symbols, so they cannot create a character absent from the source. A single
    character also cannot be formed by joining two existing characters.
    """
    observed = set(text)
    for start, stop in ((0xE000, 0xF900), (0xF0000, 0xFFFFE), (0x100000, 0x10FFFE)):
        for code in range(start, stop):
            char = chr(code)
            if char not in observed:
                return char
    for code in range(0x110000):
        if 0xD800 <= code <= 0xDFFF:
            continue
        char = chr(code)
        if char not in observed and unicodedata.category(char) == "Cn":
            return char
    raise ValueError("no unused marker character")


def _protect_escaped_emphasis(text: str) -> tuple[str, dict[str, str]]:
    """Hide */_ after an odd number of slashes behind collision-free keys."""
    marker = _absent_marker_char(text) + "EM"
    saved: dict[str, str] = {}
    out: list[str] = []
    slashes = 0
    for ch in text:
        if ch == "\\":
            out.append(ch)
            slashes += 1
            continue
        if ch in "*_" and slashes % 2:
            out.pop()  # one backslash is escaping; the rest belong to the text
            key = marker + str(len(saved)) + "\x00"
            saved[key] = ch
            out.append(key)
        else:
            out.append(ch)
        slashes = 0
    return "".join(out), saved


def _restore_escaped_emphasis(text: str, saved: dict[str, str]) -> str:
    if not saved:
        return text
    # The first key ends with '0': its prefix sets the exact length
    # of the marker. The regex must not consume the NUL of an adjacent code/link placeholder.
    marker = next(iter(saved))[:-2]
    pattern = re.compile(re.escape(marker) + r"\d+\x00")
    return pattern.sub(lambda m: saved[m.group(0)], text)


def _latex_superscript(m: re.Match) -> str:
    return "".join(_LATEX_SUPERSCRIPT_MAP.get(ch, ch) for ch in m.group(1))

def _latex_subscript(m: re.Match) -> str:
    return "".join(_LATEX_SUBSCRIPT_MAP.get(ch, ch) for ch in m.group(1))

def _is_currency_math_match(match: re.Match) -> bool:
    body = match.group(1)
    if _CURRENCY_MATH_RE.fullmatch(body):
        return True
    # The range "$100/$50" is seen by the regex as "$100/$": the numeric body with
    # a separator and a digit immediately after the closing dollar sign — currency, not a formula.
    text = match.string
    return (
        body[:1].isdigit()
        and body[-1:] in ("/", "-", "–", "—")
        and match.end() < len(text)
        and text[match.end()].isdigit()
    )


def _scrub_inline_math(match: re.Match) -> str:
    # "$100" and "$50 to $100" are money, not a paired formula. An empty paired
    # the sum "$100$" also stays as-is: the currency symbol must not be lost.
    if _is_currency_math_match(match):
        return match.group(0)
    # An asterisk inside a formula is multiplication, not Markdown italics.
    return re.sub(r"\s+", " ", match.group(1).replace("&", " ").replace("*", "∗")).strip()


def _scrub_latex(text: str) -> str:
    """Raw LaTeX to Unicode. Call only after removing code, otherwise slashes in regexes/Windows paths are corrupted."""
    if "\\" not in text and "$" not in text:
        return text
    # Inline math is removed only by a tight pair "$...$"; numeric
    # content remains currency. Thus '$100, and the formula $x^2$' is not merged.
    text = _LATEX_TEXT_RE.sub(r"\1", text)
    text = _LATEX_ENV_RE.sub("", text)
    text = re.sub(r"\\\\", " ", text)
    text = re.sub(r"\\(?:quad|qquad)\b", " ", text)
    text = _INLINE_MATH_DOLLAR_RE.sub(_scrub_inline_math, text)
    text = _replace_delimited(text, "\\[", "\\]", lambda s: s)
    text = _replace_delimited(text, "\\(", "\\)", lambda s: s, allow_newlines=False)
    text = re.sub(r"\$\$(.*?)\$\$", r"\1", text, flags=re.DOTALL)
    # rac{a}{b} -> a/b (one level of nesting, the most common case)
    text = re.sub(r"\\d?frac\{([^{}]*)\}\{([^{}]*)\}", r"\1/\2", text)
    # \sqrt{x} -> √x, \sqrt[n]{x} -> ⁿ√x
    text = re.sub(r"\\sqrt\[([^\]]*)\]\{([^{}]*)\}", r"\1√\2", text)
    text = re.sub(r"\\sqrt\{([^{}]*)\}", r"√\1", text)
    text = _LATEX_SYMBOL_RE.sub(lambda m: _LATEX_SYMBOL_MAP[m.group(0)], text)
    # x^{2} / x^2 -> x², x_{2} / x_2 -> x₂, for short exponents/subscripts only,
    # so that code such as a^b is not accidentally touched in languages where it is not an exponent.
    # A deliberate tradeoff: a bare x^2 outside code is treated as an exponent; rare XOR in prose without backticks will break.
    text = re.sub(r"\^\{([0-9n+\-]{1,3})\}", _latex_superscript, text)
    text = re.sub(r"\^([0-9n])(?![0-9])", _latex_superscript, text)
    text = re.sub(r"_\{([0-9]{1,3})\}", _latex_subscript, text)
    text = re.sub(r"_([0-9])(?![0-9])", _latex_subscript, text)
    # Remaining standalone command without a known Unicode equivalent — simply
    # remove the backslash so the user does not see raw '\int'/'\mathbb', etc.
    text = re.sub(r"\\([a-zA-Z]+)", r"\1", text)
    return text

# ── List markers "- text" / "* text" at the start of a line → "• text" ──────
# Turn "-/* " markers at the start of a line into "• ": otherwise the literal "-" is visible in Telegram and "*" breaks italic formatting later.
_BULLET_MARKER_RE = re.compile(r"^([ \t]*)[-*][ \t]+", re.MULTILINE)

def _normalize_bullet_markers(text: str) -> str:
    return _BULLET_MARKER_RE.sub(lambda m: m.group(1) + "• ", text)

# Bullets merged into one paragraph (“• A ... • B ... • C”; production 22.09.2026: the model
# wrote the entire comparison list on one line), so split it by lines. The
# threshold is
# 2+ separators (i.e. already 3 items: production showed on 26.09.2026 that exactly
# three-item lists — the most common case, and with a threshold of 3 they were missed).
# Short "tea • coffee" strings (one separator) and short paragraphs are ordinary prose; leave them alone.
# Prod 07.10.2026: lines like “Telegram: • ...” (~140 characters) were not caught; threshold 200→120.
# A separator with optional spaces catches concatenation without spaces (prod 05.10.2026).
_INLINE_BULLETS_MIN_SEPS = 2
_INLINE_BULLETS_MIN_LEN = 120
_INLINE_BULLET_SEP_RE = re.compile(r"\s*[•·]\s*")
_INLINE_BULLET_START_RE = re.compile(r"\s*[•·]")

def _split_inline_bullets(text: str) -> str:
    lines = text.split("\n")
    skip = _table_block_line_indexes(lines)
    out = []
    for idx, line in enumerate(lines):
        chunks: list[str] = []
        if (
            idx not in skip
            and len(line) >= _INLINE_BULLETS_MIN_LEN
            and len(_INLINE_BULLET_SEP_RE.findall(line)) >= _INLINE_BULLETS_MIN_SEPS
            and not _is_structural_line(line)
        ):
            chunks = [c.strip() for c in _INLINE_BULLET_SEP_RE.split(line)]
            chunks = [c for c in chunks if c]
        if len(chunks) >= 2:
            if _INLINE_BULLET_START_RE.match(line):
                # The line starts with a marker: there is no introductory phrase, all chunks are items.
                out.extend("• " + chunk for chunk in chunks)
            else:
                # The first chunk is an introductory phrase ("Highlights:"), followed by bullet points.
                out.append(chunks[0])
                out.extend("• " + chunk for chunk in chunks[1:])
        else:
            out.append(line)
    return "\n".join(out)

# Service markup lines: the splitter must neither consume the heading nor extract
# list items inside a quote, nor break a markdown table row (hostile review 27.09.2026).
# The check is needed in BOTH dispatchers and BOTH rendering paths.
_STRUCT_LINE_RE = re.compile(r"^\s*(?:#{1,6}\s|>|\|)")

def _is_structural_line(line: str) -> bool:
    return bool(_STRUCT_LINE_RE.match(line))

# Numbered items merged into one paragraph ("1. ... 2. ... 3. ...", prod
# 25.09.2026: the model put all 3 reasons the sky is blue on one line) —
# split across lines. Strictly: a sequence starting with 1, with 3+ items, each
# meaningful; otherwise prose such as "see points 1. and 2. below" would be split.
_INLINE_NUMBERED_MIN_ITEMS = 3
_INLINE_NUMBERED_MIN_ITEM_LEN = 12
_NUMBERED_MARKER_RE = re.compile(r"(?<!\d)(\d{1,3})\. ")

def _split_inline_numbered(text: str) -> str:
    lines = text.split("\n")
    skip = _table_block_line_indexes(lines)
    out = []
    for idx, line in enumerate(lines):
        markers = [(m.start(), int(m.group(1))) for m in _NUMBERED_MARKER_RE.finditer(line)]
        if (
            idx not in skip
            and len(markers) >= _INLINE_NUMBERED_MIN_ITEMS
            and [num for _, num in markers] == list(range(1, len(markers) + 1))
            and not _is_structural_line(line)
        ):
            bounds = [pos for pos, _ in markers] + [len(line)]
            items = [line[bounds[i]:bounds[i + 1]].strip() for i in range(len(markers))]
            if all(len(it) >= _INLINE_NUMBERED_MIN_ITEM_LEN for it in items):
                # Put the introductory phrase before "1." ("Reasons:") on a separate line, as with bullets.
                head = line[:bounds[0]].rstrip()
                if head:
                    out.append(head)
                out.extend(items)
                continue
        out.append(line)
    return "\n".join(out)

# Markdown headings "#"/"##"/"###" → **bold text**
# Regression 18.08.2026: models write ### despite the prompt. Split ATX headings by CommonMark (hashes + space); leave C#/#tag alone.
_HEADER_MARKER_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+(.*)$", re.MULTILINE)

def _normalize_headers(text: str) -> str:
    def _repl(m: re.Match) -> str:
        content = m.group(1).rstrip()
        if not content:
            # A bare line of only "#" characters with no text has nothing to make bold,
            # just remove the marker entirely instead of leaving an empty "****".
            return ""
        if content.startswith("**") and content.endswith("**") and len(content) > 4:
            # The model already wrapped the heading text in **bold** (a common
            # edge case: "### **Important**"); wrapping it AGAIN would produce "****Important****"
            # and break asterisk pairing in Phase 3 below. Since the wrapper is already
            # if present, only remove the "#" marker itself; do not touch the rest.
            return content
        return f"**{content}**"
    return _HEADER_MARKER_RE.sub(_repl, text)

# Markdown quotes "> text" -> Telegram <blockquote>
# Same principle as _normalize_bullet_markers above: '> ' is an ordinary
# GFM quote syntax is known to models without a single word in system_prompt.py
# (it says nothing about quotes, just as it says nothing about lists; see the comment
# in _normalize_bullet_markers). Previously, the line "> text" simply went
# Telegram quotes it literally with ">" at the start. Built BEFORE HTML escaping (Phase 2),
# like the other line-by-line normalizations in this section — the wrapper itself
# <blockquote> is added here, not as a markdown marker for Phase 3, so that
# do not confuse it with an ordinary ">" inside text (for example, "5 > 3").
_BLOCKQUOTE_LINE_RE = re.compile(r"^> ?(.*)$")
#  sentinels instead of literal <blockquote>/</blockquote> for the same reason,
# the same as code-block placeholders in _md_to_html (see Phase 1 there): if inserted
# a real HTML tag here; Phase 2 (HTML-escape) escapes it as well. Sentinels
# invisible to escape (which only touches &/</>) and are replaced with real tags
# already AFTER Phase 3 — so text inside a quote still goes through the usual
# escape/markdown phase (for example, "> **important**" correctly becomes a quote with
# bold text inside), only the wrapper itself changes.
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

def _split_bare_url(url: str) -> tuple[str, str]:
    """(address without the tail, tail). Return the tail to the text: "_" is italics,
    ")" closes a parenthetical phrase, and Cyrillic continues the sentence (review 17.11.2026)."""
    cut = len(url)
    for i, ch in enumerate(url):
        if "\u0400" <= ch <= "\u04FF":
            cut = i
            break
    clean = url[:cut]
    while clean and clean[-1] in ".,;:!?…»\"'_":
        clean = clean[:-1]
    while clean.endswith(")") and clean.count(")") > clean.count("("):
        clean = clean[:-1]
    return clean, url[len(clean):]


def _md_to_html(text: str) -> str:
    """Converts Markdown-like text to Telegram HTML.

    Contract: independent regex passes; order is critical. Do not rewrite this as a parser:
    ~20 tests catch cross-phase conflicts on production traffic.

    Required phase order (changing it breaks edge cases that are already tuned):
      0. Normalize raw HTML tags (<b>/<i>/<code>/<pre> and broken self-closing tags) to
         Markdown equivalents BEFORE escaping (step 2); otherwise legitimate tags from
         the model would become visible garbage such as "&lt;b&gt;".
      1. Extract code blocks/spans (```...```/`...`) and replace them with placeholders
         BEFORE LaTeX/tables/lists/Markdown; otherwise backslashes and "|"/"-" inside
         real code (regexes, Windows paths, bitwise OR) would be corrupted.
      1.3. LaTeX → Unicode (_scrub_latex) — code was already removed in step 1.
       1.4. List markers "-"/"* " → "•" (_normalize_bullet_markers) — BEFORE tables,
            so the table separator row ("|---|---|") is handled first and is not
            mistaken for a list marker.
       1.405. Run-together "• A • B" in one paragraph → separate lines (_split_inline_bullets).
       1.406. Run-together "1. A 2. B 3. C" in one paragraph → separate lines (_split_inline_numbered).
      1.45. Markdown blockquotes "> " → sentinels BQS/BQE (_convert_
            blockquotes) — sentinels, not immediate <blockquote> tags, because Phase 2
            would escape the literal tag; the real tag is inserted after Phase 3 (see below).
      1.5. Markdown tables → bullet lists (_convert_markdown_tables_to_lists) —
           code and lists were already processed/removed in steps 1/1.4.
      2. HTML-escape the remaining text (&/</>).
      3. Markdown (**bold**/*italic*/~~strike~~/[text](url)) → HTML tags — AFTER
         escaping; otherwise the markup characters themselves might be escaped
         too early. Links [text](url) come last in this phase (after bold/
         italic) so the italic/bold regex passes do not enter href when a URL
         contains "_" (see the code comment).
      3.5. Blockquote sentinels (step 1.45) → real <blockquote> — after Phase 3,
           so Markdown inside the quote becomes HTML before the wrapper is finalized.
       4. Restore code blocks/spans from placeholders with their own
          escaping — last, so steps 2-3 do not affect them.
    """
    if not text:
        return ""

    # ── Phase 0: convert the model's raw HTML to markdown before escaping, or '&lt;b&gt;' remains visible.
    # <br> into a sentinel; leave <blockquote> untouched.
    text = re.sub(r"<br\s*/?>", "\x00BR\x00", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:b|strong|i|em|u|s|code|pre)\s*/>", "", text, flags=re.IGNORECASE)
    text = _replace_tag_pairs(text, ("<b>", "<strong>"), ("</b>", "</strong>"), lambda s: f"**{s}**")
    text = _replace_tag_pairs(text, ("<i>", "<em>"), ("</i>", "</em>"), lambda s: f"*{s}*")
    text = _replace_tag_pairs(text, ("<u>",), ("</u>",), lambda s: s)
    # spoiler uses the same trick as <u> above: system_prompt.py does not ask the model for them
    # use it, so this is purely a safety net in case the model still
    # writes a literal tag. Previously it was not caught here at all and reached Phase 2
    # of escaping — the user saw visible garbage "&lt;tg-spoiler&gt;".
    text = _replace_tag_pairs(text, ("<tg-spoiler>",), ("</tg-spoiler>",), lambda s: s)
    text = _replace_spoiler_spans(text)
    text = _replace_tag_pairs(text, ("<s>",), ("</s>",), lambda s: f"~~{s}~~")
    text = _replace_tag_pairs(text, ("<pre>",), ("</pre>",), lambda s: f"```\n{s}\n```")
    text = _replace_tag_pairs(text, ("<code>",), ("</code>",), lambda s: f"`{s}`")

    # The model leaves an unclosed ``` in a truncated answer; streaming cuts the tail
    # closed it further, and the final assembly showed the user a literal fence
    # (external audit 16.11.2026).
    if text.count("```") % 2 and not text.rstrip().endswith("```"):
        text = text.rstrip() + "\n```"

    # ── Phase 1: cut out code before processing ────────────────
    _saved: dict[str, str] = {}
    _counter = [0]

    def _save_block(m: re.Match) -> str:
        key = f"\x00CB{_counter[0]}\x00"
        _counter[0] += 1
        _saved[key] = m.group(0)
        return key

    def _save_bare_block(m: re.Match) -> str:
        clean, tail = _split_bare_url(m.group(0))
        key = f"\x00CB{_counter[0]}\x00"
        _counter[0] += 1
        _saved[key] = clean
        return key + tail

    text = _FENCE_BLOCK_RE.sub(_save_block, text)
    text = re.sub(r"`[^`\n]+`", _save_block, text)
    # Extract links as code: "_" in a URL breaks italic/bold. Markdown inside label is not supported.
    # Parentheses in a URL form one balanced pair (wiki titles such as "X_(1993)"); double
    # nesting is deliberately unsupported: there are no real titles with it, and recursive
    # the regex risks backtracking (review 16.11.2026).
    text = re.sub(r"\[([^\[\]]+)\]\((https?://(?:[^\s()]|\([^()\s]*\))+)\)", _save_block, text)
    # Hide bare URLs before inline markup: "__b__" and "*x*" in a path are part
    # addresses, not bold/italic (external audit 16.11.2026).
    text = re.sub(r"https?://[^\s<>\"'`*~|]+", _save_bare_block, text)
    text, _escaped_emphasis = _protect_escaped_emphasis(text)

    # Phase 1.3: raw LaTeX -> Unicode (see _scrub_latex above); the code is already
    # was moved in the previous step, so backslashes in the real code
    # (regexes, Windows paths, etc.) are unaffected.
    text = _scrub_latex(text)

    # ── Phase 1.4: list markers "- "/"* " → "• " (see _normalize_bullet_
    # markers above) BEFORE tables and BEFORE Phase 3, to avoid confusion with "**bold**" and
    # so the table separator row ("|---|---|") is processed first.
    text = _normalize_bullet_markers(text)

    # Phase 1.405: joined "• A • B" in one paragraph → separate lines (see
    # _split_inline_bullets above)—after marker normalization, before tables/escape.
    text = _split_inline_bullets(text)

    # ── Phase 1.406: merged "1. A 2. B 3. C" in one paragraph → separate lines
    # (see _split_inline_numbered above): the code was already extracted in step 1, and numbering
    # versions/dates ("3.5", "1995.") do not match the rule (a sequence starting with 1 is required).
    text = _split_inline_numbered(text)

    # ── Phase 1.41: markdown headings “#”/“##”/“###” → **bold text** (see
    # _normalize_headers above) — after lists, before HTML escaping and before Phase 3
    # (the resulting "**...**" is handled by the regular bold regex in the common
    # grounds; a separate branch is unnecessary).
    text = _normalize_headers(text)

    # ── Phase 1.45: Markdown quotes "> " → blockquote sentinels (see
    # _convert_blockquotes above)—BEFORE HTML escaping, because deciding "this
    # a quote line" is accepted by a literal "> " at the start of the line; the
    # the <blockquote> wrapper is inserted later (after Phase 3), while sentinels
    # (BQS/BQE) escaping in Phase 2 does not touch.
    text = _convert_blockquotes(text)

    # Phase 1.5: markdown tables → a list of items (see _convert_markdown_
    # tables_to_lists above) — the code was already extracted in the previous step, so "|"
    # inside code (for example, bitwise OR in Rust/C) does not reach here.
    text = _convert_markdown_tables_to_lists(text)
    # A table tail without a header: after the main parse, so we do not touch
    # recognized tables (external audit 16.11.2026).
    text = _convert_orphan_table_rows(text)

    # Phase 2: escape everything else in HTML
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # ── Phase 3: apply markdown ────────────────────────────────────────────────────────
    # Handle triple emphasis first; otherwise "***x***" produced crossed
    # <b><i>…</b></i>; Telegram rejected this HTML and the entire chunk lost its
    # markup
    # (external audit 16.11.2026).
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text, flags=re.DOTALL)
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    # Underscores inside a word indicate a filename or URL, not italics: a word boundary,
    # as in _strip_markdown (external audit 16.11.2026).
    text = re.sub(r"(?<!\w)__([^\n]+?)__(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"\*(.*?)\*", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.*?)~~", r"<s>\1</s>", text)

    # ── Phase 3.5: blockquote sentinels → real <blockquote> ─────────────
    # After Phase 3, so Markdown inside a quote (for example "> **important**")
    # turned into HTML before the wrapper became a real tag.
    text = text.replace(_BLOCKQUOTE_START, "<blockquote>").replace(_BLOCKQUOTE_END, "</blockquote>")
    text = _restore_escaped_emphasis(text, _escaped_emphasis)

    # ── Phase 4: restore code with escaping ────────────────────
    # Links first: a link label may contain a code placeholder
    # ([`code` text](url) — code is removed before the link), the late code replacement
    # finds it inside the inserted <a>, and there is no CB0 leak.
    for key, orig in _saved.items():
        if not orig.startswith("["):
            continue
        m = re.match(r"\[([^\[\]]+)\]\((https?://(?:[^\s()]|\([^()\s]*\))+)\)", orig, re.DOTALL)
        label, url = m.group(1), m.group(2)
        label, escaped = _protect_escaped_emphasis(label)
        label = _safe_link_label(label, url)
        label = _restore_escaped_emphasis(label, escaped)
        label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        url = url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        text = text.replace(key, f'<a href="{url}">{label}</a>')
    for key, orig in _saved.items():
        if orig.startswith("["):
            continue
        if orig.startswith("http"):
            # Bare link: return it unchanged; only escape HTML characters.
            replacement = orig.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        elif orig.startswith("```"):
            m = _FENCE_UNWRAP_RE.match(orig)
            lang, inner = (m.group(1), m.group(2)) if m else ("", orig[3:-3])
            inner = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            # language is an entity "pre" attribute in Telegram (enables syntax highlighting
            # in clients that support it); previously the language was stripped from ```python
            # by the regex on save, but never reached the output.
            replacement = f'<pre><code class="language-{lang}">{inner}</code></pre>' if lang else f"<pre>{inner}</pre>"
        else:
            inner = orig[1:-1]
            inner = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            replacement = f"<code>{inner}</code>"
        text = text.replace(key, replacement)

    # The <br> sentinel (see Phase 0): on the normal HTML path, it is a line break.
    # Caveat: <br> INSIDE code blocks will also become a line break (Phase 0
    # runs before code extraction) — the same class of tradeoff already present in
    # <b>/<i> higher up in the code; code examples with a literal <br> are rare.
    text = text.replace("\x00BR\x00", "\n")

    return text


def _rich_inline(text: str) -> str:
    """Inline markup for rich-block internals (headings, table cells): the same
    escape + **bold**/*italic*/~~strike~~ as Phase 2–3 in _md_to_html.
    Code/link placeholders are intentionally left alone;
    they are restored later by the shared final pass, as in _md_to_html."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Triple emphasis first: otherwise "***x***" produced crossed
    # <b><i>…</b></i>, which Telegram rejected, causing the entire chunk to lose its markup
    # (external audit 16.11.2026).
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text, flags=re.DOTALL)
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    # Underscores inside a word indicate a filename or URL, not italics: the word boundary,
    # as in _strip_markdown (external audit 16.11.2026).
    text = re.sub(r"(?<!\w)__([^\n]+?)__(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"\*(.*?)\*", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.*?)~~", r"<s>\1</s>", text)
    return text


def _build_rich_table(header: list[str], rows: list[list[str]], *, split: bool = True) -> str:
    """Builds <table bordered> from parsed cells (see _split_table_cells).
    Cells go through _rich_inline, which supports **bold**/*italic*/`code`
    (code placeholders are expanded by the shared final pass later)."""
    def render(indexes: list[int], *, compact: bool) -> str:
        attr = " bordered compact" if compact else " bordered"
        parts = [f"<table{attr}>"]
        parts.append("<tr>" + "".join(f"<th>{_rich_inline(header[i])}</th>" for i in indexes) + "</tr>")
        for row in rows:
            cells = [row[i] if i < len(row) else "" for i in indexes]
            parts.append("<tr>" + "".join(f"<td>{_rich_inline(c)}</td>" for c in cells) + "</tr>")
        parts.append("</table>")
        return "".join(parts)

    if not split or len(header) <= 3:
        return render(list(range(len(header))), compact=len(header) > 3)

    # Production 24.11.2026: four or more columns ran past the right edge of the chat.
    # Split into blocks of up to three columns, repeating the first as the row label.
    tables: list[str] = []
    for start in range(1, len(header), 2):
        indexes = [0, *range(start, min(start + 2, len(header)))]
        tables.append(render(indexes, compact=True))
    split = "".join(tables)
    # Repeating a row inflates the HTML and block count. We keep a large table
    # one compact table: better horizontal scrolling than losing rich formatting.
    if (len(rows) + 1) * len(tables) > 500 or _tg_len(split) > 4096:
        return render(list(range(len(header))), compact=True)
    return split


def _md_to_rich_html(text: str, *, split_tables: bool = True) -> str:
    """A variant of _md_to_html for Rich Messages (Bot API 10.1+, sendRichMessage):
    Telegram renders structural blocks itself, so the three restrictions of the
    regular path do not apply: Markdown tables become real <table> elements,
    headings become <h2>/<h3>/<h4>, and LaTeX ($...$/$$...$$) remains raw text in
    <tg-math>/<tg-math-block> (NOT a Unicode substitution, as in _md_to_html).
    Everything else matches _md_to_html 1:1: the same Phase 0 (raw model HTML),
    the same code/link placeholders, quotes, lists and escaping, and the same
    restoration order (code last). Keep this in sync with _md_to_html when
    changing escaping/Phase 0: a mismatch renders the stream (HTML) and final
    version (rich) of the same answer differently.
    The regular (non-rich) path is unchanged: stream edits still use _md_to_html,
    and sendRichMessage falls back to it on an API error (see bot.py)."""
    if not text:
        return ""

    # The same <br> sentinel as in _md_to_html above, but expanded to <br/>
    # (a valid rich line-break tag, including inside <td> cells).
    text = re.sub(r"<br\s*/?>", "\x00BR\x00", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:b|strong|i|em|u|s|code|pre)\s*/>", "", text, flags=re.IGNORECASE)
    text = _replace_tag_pairs(text, ("<b>", "<strong>"), ("</b>", "</strong>"), lambda s: f"**{s}**")
    text = _replace_tag_pairs(text, ("<i>", "<em>"), ("</i>", "</em>"), lambda s: f"*{s}*")
    text = _replace_tag_pairs(text, ("<u>",), ("</u>",), lambda s: s)
    text = _replace_tag_pairs(text, ("<tg-spoiler>",), ("</tg-spoiler>",), lambda s: s)
    text = _replace_spoiler_spans(text)
    text = _replace_tag_pairs(text, ("<s>",), ("</s>",), lambda s: f"~~{s}~~")
    text = _replace_tag_pairs(text, ("<pre>",), ("</pre>",), lambda s: f"```\n{s}\n```")
    text = _replace_tag_pairs(text, ("<code>",), ("</code>",), lambda s: f"`{s}`")

    # Close an unclosed fence in the rich path too: otherwise the final message
    # showed "```python" literally (external audit 16.11.2026).
    if text.count("```") % 2 and not text.rstrip().endswith("```"):
        text = text.rstrip() + "\n```"

    _code: dict[str, str] = {}
    _links: dict[str, str] = {}
    _bare: dict[str, str] = {}
    _math: dict[str, tuple[str, bool]] = {}
    _tables: dict[str, tuple[list[str], list[list[str]]]] = {}
    _heads: dict[str, tuple[int, str]] = {}
    _counter = [0]

    def _take(store: dict, prefix: str, value) -> str:
        key = f"\x00{prefix}{_counter[0]}\x00"
        _counter[0] += 1
        store[key] = value
        return key

    text = _FENCE_BLOCK_RE.sub(lambda m: _take(_code, "CB", m.group(0)), text)
    text = re.sub(r"`[^`\n]+`", lambda m: _take(_code, "CB", m.group(0)), text)
    text = re.sub(r"\[([^\[\]]+)\]\((https?://(?:[^\s()]|\([^()\s]*\))+)\)", lambda m: _take(_links, "CB", m.group(0)), text)
    # Hide bare URLs in the rich path too: "__b__" in a path is an address, not bold.
    text = re.sub(r"https?://[^\s<>\"'`*~|]+",
                  lambda m: _take(_bare, "CB", _split_bare_url(m.group(0))[0]) + _split_bare_url(m.group(0))[1],
                  text)
    text, _escaped_emphasis = _protect_escaped_emphasis(text)
    # Bracket LaTeX forms (a real nemotron case: \[ S = \pi r^{2} \]): the same
    # meaning $/$$ has higher priority and is extracted before them, so $ inside is not parsed.
    text = _replace_delimited(text, "\\[", "\\]", lambda s: _take(_math, "MM", (s, True)), min_len=1)
    text = _replace_delimited(text, "\\(", "\\)", lambda s: _take(_math, "MM", (s, False)),
                               allow_newlines=False)
    text = re.sub(r"\$\$([\s\S]+?)\$\$", lambda m: _take(_math, "MM", (m.group(1), True)), text)
    # Inline format requires content without edge whitespace (as in GFM): otherwise
    # prices such as "$50 to $100" would become a "formula". Standalone "$80 000"
    # without a closing character do not match anyway.
    def _take_inline_math(m: re.Match) -> str:
        if _is_currency_math_match(m):
            return m.group(0)
        return _take(_math, "MM", (m.group(1), False))

    text = _INLINE_MATH_DOLLAR_RE.sub(_take_inline_math, text)

    def _take_heading(m: re.Match) -> str:
        # The same tolerance as the legacy _HEADER_MARKER_RE (leading spaces, up to 6
        # hash marks); per-chat scale: # → h2, ## → h3, everything else → h4.
        level = len(m.group(1))
        tag_level = 2 if level == 1 else (3 if level == 2 else 4)
        # Scrub residual LaTeX in the heading immediately: the heading goes into a placeholder
        # BEFORE the general _scrub_latex below, and the user saw "\alpha" in <h3> instead of α
        # (adversarial review 27.09.2026).
        return _take(_heads, "HD", (tag_level, _scrub_latex(m.group(2).strip())))

    text = re.sub(r"^[ \t]*(#{1,6})[ \t]+(.+)$", _take_heading, text, flags=re.MULTILINE)

    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if "|" in line and i + 1 < n and "-" in lines[i + 1] and _is_table_separator(lines[i + 1], line):
            header_cells = _split_table_cells(line)
            if len(header_cells) >= 2:
                data_rows: list[list[str]] = []
                j = i + 2
                while j < n and "|" in lines[j] and lines[j].strip():
                    data_rows.append(_split_table_cells(lines[j]))
                    j += 1
                if data_rows:
                    out.append(_take(_tables, "TB", (header_cells, data_rows)))
                    i = j
                    continue
        out.append(line)
        i += 1
    text = _convert_orphan_table_rows("\n".join(out))

    # Remaining LaTeX OUTSIDE delimiters ($…$, \[…\]) → Unicode, as in the normal path.
    # Previously, the rich path did not touch it, and the user saw "\frac{1}{2}" and "\alpha\times\beta"
    # in the final message, while the stream/HTML fallback showed 1/2 and α×β (audit 26.09.2026).
    # Code/math/table placeholders are safe: they contain neither slashes nor "^"/"_".
    text = _scrub_latex(text)

    text = _normalize_bullet_markers(text)
    # The same separation of merged lists as in the ordinary path (Phase 1.405/1.406):
    # otherwise the stream (HTML) and final output (rich) of the same answer look different.
    text = _split_inline_bullets(text)
    text = _split_inline_numbered(text)
    text = _convert_blockquotes(text)

    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Triple emphasis first: otherwise "***x***" produced overlapping
    # <b><i>…</b></i>, Telegram rejected such HTML and the entire chunk lost its formatting
    # (external audit 16.11.2026).
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text, flags=re.DOTALL)
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    # Underscores inside a word are a filename or URL, not italics: a word boundary,
    # as in _strip_markdown (external audit 16.11.2026).
    text = re.sub(r"(?<!\w)__([^\n]+?)__(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"\*(.*?)\*", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.*?)~~", r"<s>\1</s>", text)

    text = text.replace(_BLOCKQUOTE_START, "<blockquote>").replace(_BLOCKQUOTE_END, "</blockquote>")

    # Prod 07.10.2026: in a rich message, a bare newline is NOT a break — Telegram
    # joins paragraphs, and a spread-out list arrived as one blob (in normal
    # HTML paths support
    # , so the bug was visible only in the final version). Line breaks
    # replace them with <br/> — exactly as the docs show them for <blockquote>.
    # Moment: code/links/tables/headings are still in placeholders (without
    #  inside),
    # so multiline <pre> blocks are unaffected.
    text = text.replace("\n", "<br/>")

    for key, (tag_level, inner) in _heads.items():
        text = text.replace(key, f"<h{tag_level}>{_rich_inline(inner)}</h{tag_level}>")
    for key, (header, rows) in _tables.items():
        text = text.replace(key, _build_rich_table(header, rows, split=split_tables))
    for key, (latex, block) in _math.items():
        latex = latex.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        tag = "tg-math-block" if block else "tg-math"
        text = text.replace(key, f"<{tag}>{latex}</{tag}>")
    # EM placeholders could end up inside a heading, table, or formula.
    text = _restore_escaped_emphasis(text, _escaped_emphasis)
    for key, orig in _links.items():
        m = re.match(r"\[([^\[\]]+)\]\((https?://(?:[^\s()]|\([^()\s]*\))+)\)", orig, re.DOTALL)
        label, url = m.group(1), m.group(2)
        label, escaped = _protect_escaped_emphasis(label)
        label = _safe_link_label(label, url)
        label = _restore_escaped_emphasis(label, escaped)
        label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        url = url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        text = text.replace(key, f'<a href="{url}">{label}</a>')
    for key, orig in _bare.items():
        text = text.replace(key, orig.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    for key, orig in _code.items():
        if orig.startswith("```"):
            m = _FENCE_UNWRAP_RE.match(orig)
            lang, inner = (m.group(1), m.group(2)) if m else ("", orig[3:-3])
            inner = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            replacement = f'<pre><code class="language-{lang}">{inner}</code></pre>' if lang else f"<pre>{inner}</pre>"
        else:
            inner = orig[1:-1]
            inner = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            replacement = f"<code>{inner}</code>"
        text = text.replace(key, replacement)

    text = text.replace("\x00BR\x00", "<br/>")

    return text


def _strip_markdown(text: str) -> str:
    """Strip markdown formatting into plain text for the last line of defense when sending
    (plain text without parse_mode): otherwise, if HTML/rich formatting fails, raw
    `**`, backticks and `[label](url)` reach the chat (production case 17.09.2026 — an answer with `**`
    arrived as-is). It removes syntax only, not words."""
    if not text:
        return ""
    # Hide bare URLs before cleanup: "_" inside them is not emphasis.
    _urls: dict[str, str] = {}
    _code: dict[str, str] = {}
    def _save_url(m: re.Match) -> str:
        clean, tail = _split_bare_url(m.group(0))
        key = f"\x00U{len(_urls)}\x00"
        _urls[key] = clean
        return key + tail
    def _save_code(m: re.Match) -> str:
        key = f"\x00C{len(_code)}\x00"
        _code[key] = m.group(1)
        return key
    text = _FENCE_CONTENT_RE.sub(_save_code, text)
    text = re.sub(r"`([^`\n]+)`", _save_code, text)
    text = re.sub(r"\[([^\[\]]+)\]\((https?://(?:[^\s()]|\([^()\s]*\))+)\)", r"\1", text)
    text = re.sub(r"https?://[^\s<>\"'`*~|]+", _save_url, text)
    text, _escaped_emphasis = _protect_escaped_emphasis(text)
    text = _scrub_latex(text)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text, flags=re.DOTALL)
    # A lone '_' only outside a word: do not touch my_file_name or file names.
    text = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Restore EM before code/URL: their original text may contain the same
    # the marker, and a late replacement would corrupt the data (review I25, round 4).
    text = _restore_escaped_emphasis(text, _escaped_emphasis)
    for key, code in _code.items():
        text = text.replace(key, code)
    for key, url in _urls.items():
        text = text.replace(key, url)
    return text


def _tg_len(text: str) -> int:
    """Length as measured by Telegram: UTF-16 code units, not code points (an emoji
    outside the BMP takes 2 units, so 3,000 of them already overflow despite
    having only 3,000 "short" characters)."""
    return len(text.encode("utf-16-le")) // 2


def _take_prefix_by_units(text: str, max_units: int) -> str:
    """The longest prefix is no longer than max_units (do not split a code point)."""
    if _tg_len(text) <= max_units:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _tg_len(text[:mid]) <= max_units:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


_FENCE_LANG = r"[a-zA-Z0-9_+#.-]*"
_FENCE_BLOCK_RE = re.compile(rf"```{_FENCE_LANG}\n.*?\n```", re.DOTALL)
_FENCE_CONTENT_RE = re.compile(rf"```{_FENCE_LANG}\n(.*?)\n```", re.DOTALL)
_FENCE_UNWRAP_RE = re.compile(rf"\A```({_FENCE_LANG})\n(.*)\n```\Z", re.DOTALL)
# Do not split below this floor: pathological syntax could theoretically inflate
# HTML without end; an endless bisect is worse than one oversized chunk with a fallback.
_SPLIT_FLOOR_UNITS = 64


def _chunk_plain(span: str, max_len: int) -> list[str]:
    """Pieces of a plain segment along paragraph/line/sentence boundaries (in units)."""
    if _tg_len(span) <= max_len:
        return [span]
    chunks: list[str] = []
    remaining = span
    while _tg_len(remaining) > max_len:
        window = _take_prefix_by_units(remaining, max_len)
        cut = -1
        for sep in ("\n\n", "\n", ". ", " "):
            idx = window.rfind(sep)
            if idx > len(window) * 0.5:
                cut = idx + len(sep)
                break
        if cut <= 0:
            cut = len(window)
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _chunk_fence_block(block: str, max_len: int) -> list[str]:
    """The entire fence if it fits; otherwise line by line, reopening the fence."""
    if _tg_len(_md_to_html(block)) <= max_len:
        return [block]
    m = _FENCE_UNWRAP_RE.match(block)
    if not m:
        return _chunk_plain(block, max_len)
    lang, inner = m.group(1), m.group(2)
    wrap_overhead = _tg_len(f"```{lang}\n") + _tg_len("\n```")
    inner_chunks = _chunk_plain(inner, max(64, max_len - wrap_overhead)) or [inner]
    return [f"```{lang}\n{part}\n```" for part in inner_chunks]


def _split_to_fit_html(chunk_md: str, max_len: int) -> list[str]:
    """Split a Markdown chunk until its final HTML fits the limit."""
    if _tg_len(_md_to_html(chunk_md)) <= max_len:
        return [chunk_md]
    m = _FENCE_UNWRAP_RE.match(chunk_md)
    wrap = (m.group(1), True) if m else None
    inner = m.group(2) if m else chunk_md
    if not inner.strip():
        return [chunk_md]
    lines = inner.split("\n")
    if len(lines) > 1:
        mid = len(lines) // 2
        parts = ["\n".join(lines[:mid]), "\n".join(lines[mid:])]
    else:
        half = _take_prefix_by_units(inner, _tg_len(inner) // 2) or inner[:1]
        rest = inner[len(half):]
        parts = [half, rest] if rest else [half]
    out: list[str] = []
    for part in parts:
        piece = f"```{wrap[0]}\n{part}\n```" if wrap else part
        if _tg_len(part) <= _SPLIT_FLOOR_UNITS:
            out.append(piece)
        else:
            out.extend(_split_to_fit_html(piece, max_len))
    return out


def _merge_short_chunks(chunks: list[str], max_len: int) -> list[str]:
    """Join adjacent chunks back together while they fit: otherwise fence-based segmentation
    would split every short message containing code into three.
    """
    merged: list[str] = []
    acc = ""
    for chunk in chunks:
        candidate = chunk if not acc else acc + chunk
        if _tg_len(_md_to_html(candidate)) <= max_len:
            acc = candidate
        else:
            if acc:
                merged.append(acc)
            acc = chunk
    if acc or not merged:
        merged.append(acc)
    return merged


def _split_text_chunks(text: str, max_len: int = 4096) -> list[str]:
    """Splits long text into parts no longer than max_len, preferring paragraph, line, or sentence boundaries rather than cutting through words. Messages over Telegram's limit (4096 characters) previously went unsent, so users saw nothing.

Extracted from bot.py (monolith split, September 2026): a pure string function with no Telegram or runtime dependencies, like _md_to_html above. The 4096 default duplicates TG_MAX_LEN from bot.py verbatim (importing that constant would create a circular import): both locations track Telegram's limit and must change with it.

The limit is enforced twice: the markdown itself is split in units (non-BMP emoji count as 2), then each chunk is checked against the length of the resulting HTML. Markup expands text, so a "4095-character" chunk could otherwise get a 400 response from Telegram. Code fences are not split; long ones are divided line by line and reopened."""
    if not text:
        return [text]
    pieces: list[tuple[bool, str]] = []
    pos = 0
    for m in _FENCE_BLOCK_RE.finditer(text):
        if m.start() > pos:
            pieces.append((False, text[pos:m.start()]))
        pieces.append((True, m.group(0)))
        pos = m.end()
    if pos < len(text):
        pieces.append((False, text[pos:]))
    if not pieces:
        return [text]
    md_chunks: list[str] = []
    for is_fence, seg in pieces:
        if is_fence:
            md_chunks.extend(_chunk_fence_block(seg, max_len))
        else:
            # Separate stuck-together bullets BEFORE splitting: otherwise the chunker tears the list
            # by “. ” and chunks with one marker miss the splitter threshold (production 07.10.2026).
            seg = _split_inline_numbered(_split_inline_bullets(seg))
            md_chunks.extend(_chunk_plain(seg, max_len))
    out: list[str] = []
    for chunk in md_chunks:
        out.extend(_split_to_fit_html(chunk, max_len))
    return _pull_dangling_headings(_merge_short_chunks(out, max_len) or [text], max_len)


# A line that must not remain last in the bubble: a markdown heading,
# an entirely bold line, a numbered section ("III. Results"), a separator, or
# a short line ending with a colon.
_DANGLING_HEAD_RE = re.compile(
    r"^\s*(?:#{1,6}\s"
    r"|\*\*[^*\n]{1,60}\*\*[:：]?\s*"
    r"|(?:[IVXLC]{1,4}|\d{1,2})[.)]\s+\S[^.!?]{0,55}$"
    r"|(?:---+|———+|___+)\s*$"
    r"|\S[^.!?]{0,55}[:：]\s*$)"
)


def _pull_dangling_headings(chunks: list[str], max_len: int) -> list[str]:
    """A chunk must not end with a heading or divider without a body: in a live
    response on 11.11.2026, the bubble ended with “III. Results” while the actual losses
    went into the next one. Claude places the heading above its text. Move
    only a single short line separated by a blank line — do not move an ordinary
    paragraph without a final period."""
    if len(chunks) < 2:
        return chunks
    out = list(chunks)
    for i in range(len(out) - 1):
        head_part, sep, tail_part = out[i].rpartition("\n\n")
        if not sep:
            continue
        candidate = tail_part.strip()
        if not candidate or "\n" in candidate or len(candidate) > 60:
            continue
        if not _DANGLING_HEAD_RE.match(candidate):
            continue
        if not head_part.strip():
            continue
        joined = candidate + "\n\n" + out[i + 1].lstrip("\n")
        if _tg_len(joined) <= max_len:
            out[i] = head_part.rstrip()
            out[i + 1] = joined
        else:
            # It does not fit whole: the heading still moves forward, and the next
            # the chunk is cut so that the heading remains ABOVE the first paragraph
            # (otherwise the result would be a bubble containing only "## Summary").
            room = max(16, max_len - _tg_len(candidate) - 2)
            subs = _split_to_fit_html(out[i + 1].strip(), room) or [out[i + 1].strip()]
            subs[0] = candidate + "\n\n" + subs[0]
            out[i] = head_part.rstrip()
            out[i + 1:i + 2] = subs
    return out


def _truncate_html_to_fit(md_text: str, limit: int) -> str:
    """Cut HTML at a source-text boundary, not a tag boundary: slicing finished HTML
    by codepoint can land inside <b>/a link — Telegram will reply
    "can't parse entities" (AUD-J-002). Binary search finds the longest
    source prefix whose HTML fits the limit. Extracted from bot.py (guest
    answers are a single InlineQueryResultArticle with no plain-text fallback)."""
    full = _md_to_html(md_text)
    if _tg_len(full) <= limit:
        return full
    lo, hi = 0, len(md_text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _tg_len(_md_to_html(md_text[:mid])) <= limit - 1:
            lo = mid
        else:
            hi = mid - 1
    # Telegram counts limits in UTF-16 units, not characters: an emoji takes
    # two. Slicing by len() produced HTML longer than the limit and a 400 on the guest response,
    # where there is no fallback to plain text (corpus check 05.11.2026).
    return _take_prefix_by_units(_md_to_html(md_text[:lo]), limit - 1) + "…"

