"""
lumen_state_storage.py — низкоуровневый слой персистентности: клиент Upstash Redis
REST API, единая точка ветвления backend'а (Upstash vs локальный файл), сериализация
одного чата в JSON-совместимый снимок и вспомогательные path/key-хелперы для per-chat
хранилища.

Вынесено из bot.py при разбиении на модули (см. README, аудит техдолга). В отличие от
"что вообще считается грязным и когда его сбрасывать" (dirty-tracking, периодический
flush-цикл, сами словари chat_state/GLOBAL_QUOTA) — это состояние читается и мутируется
из ~30 несвязанных мест по всему bot.py (каждый обработчик сообщения, /reset, /stats,
ask_gemini/ask_openrouter_*, TTS и т.д.) и остаётся там; выносить его сюда означало бы
не разделение ответственности, а искусственное разрывание того, что по сути является
одним связным куском состояния приложения. По той же причине `_save_chat_to_storage`/
`_delete_chat_storage` (оркестрация "сериализовать + записать + поймать исключение")
ТОЖЕ остаются в bot.py как полноценные (не тонкие обёрточные) реализации, а не здесь —
они вызывают bot.py-шные `_storage_write_text`/`_storage_delete_text` ПО ИМЕНИ,
разрешаемому в пространстве имён bot.py на момент вызова, что единственный способ, по
которому существующие тесты (патчащие `bot._storage_write_text` через `unittest.mock.
patch`) продолжают перехватывать вызов — если бы эти две функции жили здесь, они бы
использовали СВОЮ собственную, непропатченную копию этих функций.

Здесь — только МЕХАНИКА хранения (как записать/прочитать/удалить текст по ключу+пути,
как сериализовать словарь одного чата в JSON-совместимый снимок), без собственных
module-level globals, завязанных на конкретный чат/квоту: конфигурация backend'а
(Upstash-креды или директория на диске) передаётся параметром `StorageConfig` на каждый
вызов, а не читается из скрытого состояния этого модуля — иначе тесты, подменяющие
`bot.UPSTASH_REDIS_REST_URL`/`bot._CHATS_DIR` и т.п. "на лету", перестали бы работать.
bot.py держит тонкие обёртки с ТЕМИ ЖЕ именами и (за вычетом добавленного
`StorageConfig` там, где он был неявным) сигнатурами — см. секцию "хранение состояния и
квот" в bot.py.
"""

from __future__ import annotations

import contextlib
import json
import logging
import urllib.parse
import urllib.request as _urllib_request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("bot")

CHAT_STATE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StorageConfig:
    """Снимок конфигурации backend'а хранилища на момент ОДНОГО вызова — собирается
    заново вызывающим кодом в bot.py на каждый вызов (см. докстринг модуля), а не
    кэшируется здесь, чтобы подмена `bot.UPSTASH_REDIS_REST_URL`/`bot._CHATS_DIR` и
    т.п. в тестах (или, в перспективе, смена конфигурации без рестарта) применялась
    сразу же, без риска словить устаревшее закешированное значение."""
    use_upstash: bool
    upstash_url: str
    upstash_token: str
    chats_dir: Path


# ─────────────────── клиент Upstash Redis REST API ───────────────────

def _upstash_request(url: str, token: str, command_path: str, *, method: str = "GET", body: bytes | None = None) -> Any:
    """Синхронный запрос к Upstash Redis REST API. Намеренно на urllib.request из
    стандартной библиотеки, а не на aiohttp/отдельном SDK — не хотим тянуть новую
    pip-зависимость ради одной интеграции. Вызывается только из save_*/load_* в
    bot.py: load_* — один раз на старте до приёма трафика, save_* — уже вынесены в
    отдельный поток через asyncio.to_thread (см. _flush_dirty_state в bot.py), так
    что блокирующий вызов здесь не блокирует event loop."""
    full_url = f"{url}/{command_path}"
    req = _urllib_request.Request(full_url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "text/plain; charset=utf-8")
    with _urllib_request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _upstash_set(url: str, token: str, key: str, value: str) -> None:
    _upstash_request(url, token, f"set/{urllib.parse.quote(key, safe='')}", method="POST", body=value.encode("utf-8"))


def _upstash_get(url: str, token: str, key: str) -> str | None:
    result = _upstash_request(url, token, f"get/{urllib.parse.quote(key, safe='')}", method="GET")
    return result.get("result") if isinstance(result, dict) else None


def _upstash_delete(url: str, token: str, key: str) -> None:
    _upstash_request(url, token, f"del/{urllib.parse.quote(key, safe='')}", method="POST")


# ─────────────────── единая точка ветвления backend'а ───────────────────

def _storage_write_text(cfg: StorageConfig, key: str, path: Path, text: str) -> None:
    """Единая точка ветвления backend'а: Upstash, если настроен, иначе локальный
    файл (атомарно — через .tmp + replace, как и раньше)."""
    if cfg.use_upstash:
        _upstash_set(cfg.upstash_url, cfg.upstash_token, key, text)
        return
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(text)
    temp_path.replace(path)


def _storage_read_text(cfg: StorageConfig, key: str, path: Path) -> str | None:
    if cfg.use_upstash:
        return _upstash_get(cfg.upstash_url, cfg.upstash_token, key)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _storage_delete_text(cfg: StorageConfig, key: str, path: Path) -> None:
    """Удаляет запись из хранилища — нужно per-chat формату: когда чат вытесняется
    _prune_old_chats() в bot.py, его собственный ключ/файл должен реально исчезать,
    а не висеть бесхозно (иначе Upstash/диск постепенно накапливали бы мусор от
    давно удалённых чатов)."""
    if cfg.use_upstash:
        _upstash_delete(cfg.upstash_url, cfg.upstash_token, key)
        return
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


# ─────────────────── per-chat ключи/пути и сериализация одного чата ───────────────────

def _chat_storage_key(chat_id: int) -> str:
    return f"lumen:chat:{chat_id}"


def _chat_storage_path(cfg: StorageConfig, chat_id: int) -> Path:
    return cfg.chats_dir / f"{chat_id}.json"


def _serialize_chat_state(state: dict[str, Any]) -> dict[str, Any]:
    """Собирает JSON-сериализуемый снимок ОДНОГО чата — общая логика между
    сохранением и ручным экспортом (см. /export_state в bot.py).

    Начиная с введения автоматического роутера моделей "gemini_model"/
    "openrouter_text_model"/"chat_provider" здесь БОЛЬШЕ НЕ хранятся — раньше это
    был явный выбор пользователя через /model и /provider, теперь провайдер и
    модель подбираются заново на каждое сообщение, хранить их per-chat незачем.
    "image_model" по той же причине убран отсюда 19 августа 2026 — генерация
    изображений (см. README, "Автоматический выбор модели") тоже перешла на
    подбор модели заново на каждый вызов (`_pick_image_model` в lumen_images.py)
    вместо персистентного выбора через удалённую команду /imgmodel. Старые
    персистентные записи, где эти поля ещё есть (созданные до соответствующих
    изменений), просто тихо игнорируются при чтении — см. _restore_single_chat в
    bot.py, там нет ни одной попытки их прочитать."""
    return {
        "schema_version": CHAT_STATE_SCHEMA_VERSION,
        "history": list(state.get("history", [])),
        "quota": state.get("quota", {}),
        "recent_media_ids": {
            uid: list(dq) for uid, dq in state.get("recent_media_ids", {}).items()
        },
    }


# ─────────────────── дата для сброса дневной квоты ───────────────────

def _current_quota_day() -> str:
    """Дата (ISO, YYYY-MM-DD) для определения "новых суток" в целях сброса квоты.
    Google обнуляет дневные RPD-лимиты по полуночи Pacific Time — используем ту
    же зону, чтобы /stats не "сбрасывался" на 7-8 часов раньше или позже реального
    обнуления лимита на стороне Google. Если данные таймзоны недоступны в окружении
    (маловероятно, но встречается в урезанных Docker-образах) — тихо откатываемся
    на UTC: чуть менее точно по времени суток, но не ломает сам факт ежедневного
    сброса."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        return datetime.utcnow().date().isoformat()
