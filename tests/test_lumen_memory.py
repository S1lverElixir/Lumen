"""test_lumen_memory.py — единый лог памяти: авторы, время, медиа-ссылки, прошлые фото и ждущая просьба."""
from __future__ import annotations

import time
from types import SimpleNamespace

import lumen_memory


def _state(**extra):
    state = {"history": [], "memory_summary": "", "memory_pending": []}
    state.update(extra)
    return state


def test_format_entry_has_author_time_and_photo_marker():
    # Автор и время в каждой реплике: в группе иначе «кто что сказал» додумывается.
    entry = {
        "role": "user", "content": "кот на подоконнике", "sender": "@m1st",
        "ts": time.time(), "media": [{"file_id": "f1", "mime": "image/jpeg"}],
    }
    line = lumen_memory.format_entry(entry)
    assert line.startswith("[@m1st, ")
    assert "[фото] кот на подоконнике" in line


def test_append_user_keeps_photo_without_caption():
    # Фото без подписи — законная реплика: раньше пустой content выбрасывался.
    state = _state()
    lumen_memory.append_user(
        state, "", sender="@m1st", sender_id=1, ts=time.time(),
        media=[{"file_id": "f1", "mime": "image/jpeg"}],
    )
    assert len(state["history"]) == 1
    assert state["history"][0]["media"][0]["file_id"] == "f1"
    assert lumen_memory.format_entry(state["history"][0]).endswith("[фото]")


def test_media_ref_stores_file_id_not_bytes():
    ref = lumen_memory.media_ref(SimpleNamespace(file_id="fid", mime_type="image/jpeg"))
    assert ref == {"file_id": "fid", "mime": "image/jpeg"}
    assert "bytes" not in ref and "data" not in ref


def test_recent_media_window_30_and_max_two_photos():
    # Окно 30 сообщений и максимум 2 фото: иначе в каждый запрос уезжала галерея.
    now = time.time()
    history = []
    for i in range(40):
        media = []
        if i in (0, 15, 35, 39):
            media = [{"file_id": f"p{i}", "mime": "image/jpeg"}]
        history.append({
            "role": "user", "content": "", "sender": "@u", "ts": now,
            "media": media,
        })
    found = lumen_memory.recent_media(
        _state(history=history), "photo", window=30, max_items=2,
    )
    ids = [ref["file_id"] for _entry, ref in found]
    assert ids == ["p35", "p39"]


def test_last_media_for_user_prefers_own_in_group_and_any_in_private():
    now = time.time()
    history = [
        {"role": "user", "content": "", "sender": "@a", "sender_id": 1, "ts": now,
         "media": [{"file_id": "own", "mime": "image/jpeg"}]},
        {"role": "user", "content": "", "sender": "@b", "sender_id": 2, "ts": now + 1,
         "media": [{"file_id": "other", "mime": "image/jpeg"}]},
    ]
    state = _state(history=history)
    own = lumen_memory.last_media_for_user(state, "photo", sender_id=1, private=False)
    assert own is not None and own[1]["file_id"] == "own"
    any_sender = lumen_memory.last_media_for_user(state, "photo", sender_id=1, private=True)
    assert any_sender is not None and any_sender[1]["file_id"] == "other"


def test_pending_instruction_finds_question_before_photo():
    # «Сейчас кину фотку, что мне идёт?» → фото без подписи отвечает на просьбу,
    # а не получает служебное «опиши подробно».
    now = time.time()
    state = _state(history=[
        {"role": "user", "content": "сейчас кину фотку, что мне больше идёт?",
         "sender": "@m1st", "sender_id": 1, "ts": now - 60},
        {"role": "assistant", "content": "Давай."},
    ])
    assert lumen_memory.pending_instruction(state, 1, now=now) == "сейчас кину фотку, что мне больше идёт?"
    # Старая просьба и обычная болтовня не тянутся.
    stale = _state(history=[{
        "role": "user", "content": "кину фотку, что мне идёт?", "sender": "@m1st",
        "sender_id": 1, "ts": now - 3600,
    }])
    assert lumen_memory.pending_instruction(stale, 1, now=now) is None
    for text in ("спасибо", "вот это да", "как дела?"):
        plain = _state(history=[{
            "role": "user", "content": text, "sender": "@m1st",
            "sender_id": 1, "ts": now - 10,
        }])
        assert lumen_memory.pending_instruction(plain, 1, now=now) is None


def test_system_prompt_with_memory_labels_summary_as_data():
    # Сводка в системном промпте должна называться данными, а не новой просьбой.
    prompt = lumen_memory.system_prompt_with_memory(
        "BASE", {"memory_summary": "Пользователь любит котов."},
    )
    assert prompt.startswith("BASE")
    assert "LONG-TERM MEMORY OF THIS CHAT" in prompt
    assert "Пользователь любит котов." in prompt
    assert "not a new user request" in prompt


def test_summary_input_keeps_previous_memory_when_body_is_long():
    # Прошлая сводка не должна выпадать при обрезке километрового хвоста.
    entries = [{"role": "user", "content": "x" * 100} for _ in range(200)]
    digest = lumen_memory.summary_input("PREVIOUS FACT", entries, max_chars=500)
    assert digest.startswith("Previous memory:\nPREVIOUS FACT")
    assert "Messages leaving the short-term window:" in digest
