"""Живой прогон через @lumen_smoke_bot:меряем плавность стриминга и работу поиска.

Кадр правки ответа — это edited_message:число правок, паузы между ними и прирост
символов показывают, идёт ли набор плавно или рывками. Вывод пишется в UTF-8 файл,
а не в консоль: консоль Windows в cp1251 не выносит ✻ и кириллицу.

Запуск:  python eval/live_smoke_probe.py
Токен берётся из .env (TEST_BOT_TOKEN) и никогда не печатается.
"""
import json
import os
import re
import sys
import time
import urllib.request

OUT = os.path.join(os.environ.get("TEMP", "/tmp"), "live_smoke_probe.json")


def _token():
    for root in (os.getcwd(), os.path.expanduser("~")):
        p = os.path.join(root, ".env")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                m = re.match(r"\s*TEST_BOT_TOKEN\s*=\s*(\S+)", line)
                if m:
                    return m.group(1).strip().strip('"').strip("'")
    raise SystemExit("TEST_BOT_TOKEN not found in .env")


TOKEN = _token()
API = "https://api.telegram.org/bot{}/{}"
TARGET = "@LumenAI_bot"


def call(method, **params):
    req = urllib.request.Request(
        API.format(TOKEN, method),
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def drain(offset):
    """Все свежие обновления: отдаём (новый offset, список событий)."""
    events = []
    while True:
        out = call("getUpdates", offset=offset, limit=100,
                   allowed_updates=["message", "edited_message", "channel_post", "edited_channel_post"])
        res = out.get("result", [])
        if not res:
            return offset, events
        for upd in res:
            offset = max(offset, upd["update_id"] + 1)
            for key in ("edited_message", "message"):
                msg = upd.get(key)
                if not msg:
                    continue
                text = msg.get("text") or ""
                if not text and msg.get("rich_message"):
                    text = " ".join(str(b.get("text") or "") for b in msg["rich_message"].get("blocks", []))
                events.append({
                    "kind": key,
                    "message_id": msg.get("message_id"),
                    "chat_id": msg.get("chat", {}).get("id"),
                    "at": round(time.time(), 2),
                    "len": len(text),
                    "text": text,
                    "photo": bool(msg.get("photo")),
                })
        if len(res) < 100:
            return offset, events


def ask(offset, question, *, wait_sec=60):
    sent = call("sendMessage", chat_id=TARGET, text=question)
    if not sent.get("ok"):
        raise SystemExit(f"sendMessage failed: {sent.get('description')}")
    started = time.time()
    events = []
    while time.time() - started < wait_sec:
        offset, new = drain(offset)
        events += new
        time.sleep(0.7)
        if events and time.time() - started > 12 and events[-1]["kind"] == "message" and not events[-1]["photo"]:
            if len(events) > 1 and time.time() - started > 20:
                break
    return offset, question, events


def summarize(label, events):
    edits = [e for e in events if e["kind"] == "edited_message"]
    fresh = [e for e in events if e["kind"] == "message"]
    by_msg = {}
    for e in edits + fresh:
        by_msg.setdefault(e["message_id"], []).append(e)
    lines = [f"=== {label}: событий {len(events)}, сообщений {len(by_msg)} ==="]
    for mid, seq in sorted(by_msg.items()):
        seq.sort(key=lambda x: x["at"])
        lines.append(f"#{mid} кадров {len(seq)} (edit={sum(1 for s in seq if s['kind']=='edited_message')}) "
                     f"долгий_хвост={round(seq[-1]['at']-seq[0]['at'],1)}s фото={any(s['photo'] for s in seq)}")
        if len(seq) > 1:
            gaps = [round(seq[i + 1]["at"] - seq[i]["at"], 2) for i in range(len(seq) - 1)]
            grows = [seq[i + 1]["len"] - seq[i]["len"] for i in range(len(seq) - 1)]
            lines.append("   интервалы,c: " + str(gaps))
            lines.append("   прирост,симв: " + str(grows))
        lines.append("   финал: " + seq[-1]["text"][:600].replace("\n", " ⏎ "))
    return "\n".join(lines), by_msg


def main():
    questions = sys.argv[1:] or [
        "Привет! Подскажи, пожалуйста, кто сейчас возглавляет Центральный банк России и какая ключевая ставка?",
        "А кто был на этой должности до неё?",
    ]
    offset = 0
    _, backlog = drain(-1)
    print(f"[probe] backlog drained: {len(backlog)}")
    report = {"backlog": len(backlog)}
    for q in questions:
        offset, q, events = ask(offset, q)
        text, by_msg = summarize(q, events)
        print(text)
        report[q] = {"events": events}
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
    print(f"[probe] полная расшифровка: {OUT}")


if __name__ == "__main__":
    main()
