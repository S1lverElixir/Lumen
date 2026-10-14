"""test_lumen_devtrace.py — JSONL-след запросов и ответов для разбора багов."""
import asyncio
from types import SimpleNamespace

import bot
import lumen_devtrace
from tests.bot_test_helpers import _FakeAdminRequest


def _enable_trace(monkeypatch, tmp_path):
    monkeypatch.setenv("DEV_TRACE", "1")
    monkeypatch.setenv("DEV_TRACE_PATH", str(tmp_path / "trace.jsonl"))


def test_devtrace_disabled_by_default_writes_nothing(monkeypatch, tmp_path):
    # Сторож: без DEV_TRACE=1 файл не создаётся даже при прямом вызове.
    monkeypatch.delenv("DEV_TRACE", raising=False)
    monkeypatch.setenv("DEV_TRACE_PATH", str(tmp_path / "trace.jsonl"))
    assert lumen_devtrace.record_answer(1, user_text="hi", reply="hello") is False
    assert not (tmp_path / "trace.jsonl").exists()


def test_devtrace_answer_clips_and_redacts_secret(monkeypatch, tmp_path):
    # Защищает разбор глупостей модели (полный маршрут и ответ в файле) и
    # отсутствие секретов в нём: регрессия — сырой токен в trace-файле.
    _enable_trace(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "BOT_TOKEN", "devtrace-secret-abc")
    ok = lumen_devtrace.record_answer(
        5, user_text="devtrace-secret-abc " + "x" * 2500, route=[("groq", "m1")],
        provider="groq", model="m1", latency_ms=12, reply="y" * 5000,
    )
    assert ok is True
    raw = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert "devtrace-secret-abc" not in raw
    assert "<REDACTED>" in raw
    entries = lumen_devtrace.read_tail(10)
    assert len(entries) == 1 and entries[0]["kind"] == "answer"
    assert len(entries[0]["user"]) == 2000
    assert len(entries[0]["reply"]) == 4000
    assert entries[0]["route"] == ["groq:m1"]


def test_devtrace_read_tail_returns_last_n_and_skips_broken(monkeypatch, tmp_path):
    # Хвост для /devtrace: последние N и пропуск битых строк вместо 500.
    _enable_trace(monkeypatch, tmp_path)
    for i in range(5):
        assert lumen_devtrace.record_passive(7, f"msg {i}") is True
    with open(tmp_path / "trace.jsonl", "a", encoding="utf-8") as f:
        f.write("{broken\n")
    last = lumen_devtrace.read_tail(2)
    assert [e["text"] for e in last] == ["msg 3", "msg 4"]


def test_devtrace_endpoint_rejects_missing_or_wrong_key(monkeypatch):
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        result = asyncio.run(bot.devtrace(_FakeAdminRequest(headers={"Authorization": "Bearer wrong"})))
        assert result.status_code == 401
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_devtrace_endpoint_returns_entries(monkeypatch, tmp_path):
    # Сторож: валидный ключ отдаёт записанный след, а не пустой список.
    _enable_trace(monkeypatch, tmp_path)
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        assert lumen_devtrace.record_failure(9, user_text="q", error="boom") is True
        result = asyncio.run(bot.devtrace(_FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))
        assert result["count"] == 1
        assert result["entries"][0]["kind"] == "failure"
        assert result["enabled"] is True
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_devtrace_passive_hook_writes_background(monkeypatch, tmp_path):
    # Фон группы попадает в след: иначе разбирать влияние контекста не по чему.
    from aiogram.enums import ChatType
    _enable_trace(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "_should_only_record_passively", lambda *a, **k: True)
    monkeypatch.setattr(bot, "is_guest_message", lambda m: False)
    monkeypatch.setattr(bot, "message_mentions_bot", lambda m: False)
    msg = SimpleNamespace(
        text="фоновый текст", caption=None,
        chat=SimpleNamespace(id=777002, type=ChatType.GROUP),
        from_user=SimpleNamespace(id=456), reply_to_message=None,
    )
    asyncio.run(bot._handle_message_core(msg))
    entries = lumen_devtrace.read_tail(10)
    assert entries and entries[-1]["kind"] == "passive"
    assert "фоновый" in entries[-1]["text"]
    bot.chat_state.pop(777002, None)
