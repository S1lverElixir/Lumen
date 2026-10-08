"""
lumen_state_storage.py — механика персистентности: клиент Upstash Redis REST API,
выбор backend (Upstash/файл), сериализация одного чата в JSON-снимок.

Оркестрация записи ("сериализовать + записать + поймать") живёт в bot.py: тесты
патчат `bot._storage_write_text` по имени, переезд сюда разорвал бы подмену.
Конфиг backend передаётся параметром, а не читается из состояния модуля —
иначе тесты с подменой кред "на лету" перестали бы работать.
"""

from __future__ import annotations

import contextlib
import json
import logging
import urllib.parse
import urllib.request as _urllib_request
from dataclasses import dataclass
from datetime import datetime, timezone
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
    # Каталог chats/ мог снести внешний процесс — создаём заново, иначе запись падает.
    path.parent.mkdir(parents=True, exist_ok=True)
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
    bot.py, там нет ни одной попытки их прочитать.
    "quota" убран отсюда 26 августа 2026 (аудит техдолга) — поле было мёртвым:
    записывалось в ChatState и сериализовалось сюда, но нигде не читалось —
    реальный учёт квоты целиком живёт в модульном GLOBAL_QUOTA (bot.py), per-chat
    квота никогда фактически не использовалась. Старые записи с этим полем в
    хранилище просто тихо игнорируются при чтении, как и остальные удалённые поля
    выше."""
    return {
        "schema_version": CHAT_STATE_SCHEMA_VERSION,
        "history": list(state.get("history", [])),
        "recent_media_ids": {
            uid: list(dq) for uid, dq in state.get("recent_media_ids", {}).items()
        },
        # Ники переживают рестарт: без них после деплоя бот снова не знал бы авторов.
        "user_names": dict(state.get("user_names", {}) or {}),
        # Настенные часы для подсчёта активных в /stats: monotonic сбрасывается
        # рестартом и делал все чаты "активными", time.time() переживает его.
        "last_activity": state.get("last_activity", 0.0),
        # Язык системных сообщений чата (см. lumen_lang.py, /lang в bot.py).
        # Старым снимкам без поля соответствует "en" — см. _chat_lang в bot.py.
        "lang": state.get("lang", "en"),
    }


# ─────────────────── дата для сброса дневной квоты ───────────────────

def _quota_day_reset_in_sec() -> float:
    """Секунд до полуночи America/Los_Angeles — через столько обнулятся
    суточные счётчики. Та же зона, что у _current_quota_day выше."""
    from datetime import timedelta
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        now = datetime.now(timezone.utc)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0.0, (nxt - now).total_seconds())

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
        return datetime.now(timezone.utc).date().isoformat()
