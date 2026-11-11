"""Аудит пользовательских строк: каждый ключ, который код просит у переводов,
обязан существовать в таблице. Регресс вида `_t(chat, "progress_foo")` показал бы
человеку служебное имя вместо фразы, и тесты таблицы этого не ловят (они проверяют
ключи таблицы, а не обращения кода).

Запуск:  python eval/keys_audit.py
"""
import io
import pathlib
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import lumen_lang

ROOT = pathlib.Path(__file__).resolve().parent.parent
CALL_RE = re.compile(r"(?:_t|_lang_t|\bt)\(\s*[^,()]*,\s*\"([a-z0-9_]+)\"")
KEYS_SEEN = set()

for path in sorted(ROOT.glob("lumen_*.py")) + [ROOT / "bot.py"]:
    text = path.read_text(encoding="utf-8", errors="replace")
    for match in CALL_RE.finditer(text):
        KEYS_SEEN.add(match.group(1))

known = set(lumen_lang.STRINGS)
unknown = sorted(KEYS_SEEN - known)
print(f"ключей в обращениях кода: {len(KEYS_SEEN)}, неизвестных: {len(unknown)}")
for key in unknown:
    print("   UNKNOWN:", key)

unused = sorted(k for k in known if k not in KEYS_SEEN and not k.startswith(("profile_", "cmd_desc_")))
print(f"ключей таблицы, не найденных в прямых вызовах (могут читаться косвенно): {len(unused)}")
for key in unused[:25]:
    print("   unused?", key)
sys.exit(1 if unknown else 0)
