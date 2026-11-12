"""Сетка усилия: совпадает ли автовыбор с тем, что выбрал бы человек.

Метки ручные (детерминированная линейка, как search_trigger_corpus), судья не нужен:
здесь проверяется правило, а не вкус модели. Порог успеха — 80 % совпадений и ноль
промахов «надо думать дольше, а выбрали low».
Запуск:  python eval/effort_grid.py
"""
from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from lumen_router_config import pick_reasoning_effort  # noqa: E402

# (формулировка, ожидаемый уровень,=is_heavy,=needs_freshness)
CASES: list[tuple[str, str, bool, bool]] = [
    # Болтовня и короткие реплики: скорость важнее глубины.
    ("привет", "low", False, False),
    ("спасибо большое!", "low", False, False),
    ("ну как тебя вообще", "low", False, False),
    ("ок", "low", False, False),
    ("ага", "low", False, False),
    # Вопросы с вопросом или числом: средняя граница.
    ("кто такие викинги", "medium", False, False),
    ("сколько будет 27*43", "medium", False, False),
    ("что такое контекстное окно", "medium", False, False),
    ("почему небо голубое", "medium", False, False),
    ("переведи на английский «добрый вечер»", "medium", False, False),
    # Свежесть: средний уровень, глубокого reasoning не требуется.
    ("кто сейчас главный тренер сборной", "medium", False, True),
    ("какая ключевая ставка ЦБ России", "medium", False, True),
    ("погода в Минске на завтра", "medium", False, True),
    ("сколько стоит билет Минск Москва", "medium", False, True),
    # Тяжёлый запрос: код, статья, разбор.
    ("напиши функцию парсера на python", "high", True, False),
    ("исправь баг в этом коде", "high", True, False),
    ("напиши эссе про индустриализацию", "high", True, False),
    ("сравни два города по четырём параметрам", "high", True, False),
    ("разбери подробно этот договор", "high", True, False),
    # Дальний горизонт: архитектура, доказательство, план на шаги.
    ("спроектируй архитектуру бота и докажи выбор", "xhigh", False, False),
    ("составь план тренировок на четыре недели по шагам", "xhigh", False, False),
    ("докажи, что корень из двух иррационален", "xhigh", False, False),
    ("продумай все trade-offs между двумя схемами кэша", "xhigh", False, False),
    ("расскажи подробно про каждый этап и почему именно так, "
     "затем сравни все варианты и дай итог, а потом ещё раз проверь расчёт?"
     "? и не забудь про края?" + "и ещё много текста " * 60, "xhigh", False, False),
    # Явные просьбы про длину и глубину.
    ("коротко: сколько будет 27*43", "low", False, False),
    ("ответь одним словом: столица Австралии — Канберра?", "low", False, False),
    ("кратко, без пояснений: что такое JWT", "low", False, False),
    ("подумай как следует и перепроверь: улитка в колодце 30 метров", "high", False, False),
    # Человек ловит модель на ошибке и просит перепроверить: ступень вверх
    # (замер сетки 12.11.2026 давал medium, пока метка не была снята с
    # прежнего плоского правила «короче 40 знаков»).
    ("реши ещё раз, ты не прав, перепроверь расчёт", "high", False, False),
    ("внимательно посчитай, сколько стоит 3 билета по 1290 со скидкой 15%", "high", False, False),
    # Многоязычные: тот же смысл по-другому.
    ("hi there", "low", False, False),
    ("why is the sky blue", "medium", False, False),
    ("write a python function that parses csv", "high", True, False),
    ("compare all three options step by step and prove it", "xhigh", False, False),
    ("briefly: what is JWT", "low", False, False),
    ("deux fois deux font combien", "medium", False, False),
    ("qué es un índice b-tree", "medium", False, False),
    ("wat is de hoofdstad van Frankrijk", "medium", False, False),
    ("was ist ein JWT kurz", "low", False, False),
    ("berapa harga tiket hari ini", "medium", False, True),
    ("kiut a költő? röviden", "low", False, False),
    ("что такое JWT, сегодня актуально?", "medium", False, True),
    ("сколько будет 27*43, внимательно", "high", False, False),
    ("объясни как ребёнку, что такое рефакторинг кода", "high", True, False),
    ("план на неделю по шагам для запуска сайта", "xhigh", False, False),
    ("оптимизируй этот алгоритм и доказательство приведи", "xhigh", False, False),
]


def main() -> int:
    bad: list[tuple[str, str, str]] = []
    under: list[str] = []
    for text, expected, heavy, fresh in CASES:
        got = pick_reasoning_effort(text, is_heavy=heavy, needs_freshness=fresh)
        if got != expected:
            bad.append((text[:44], expected, got))
            order = ["minimal", "low", "medium", "high", "xhigh"]
            if order.index(got) < order.index(expected):
                under.append(text[:44])
    total = len(CASES)
    print(f"совпадений {total - len(bad)}/{total} = {(total - len(bad)) / total * 100:.0f} %")
    for text, expected, got in bad:
        print(f"  промах: ждали {expected}, получили {got} · {text!r}")
    print(f"промахов «думает меньше, чем надо»: {len(under)}")
    for text in under:
        print(f"  недо-усилие: {text!r}")
    print("порог: >=80 % и ноль недо-усилия ->",
          "ХОРОШО" if (total - len(bad)) / total >= 0.8 and not under else "ПЛОХО")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
