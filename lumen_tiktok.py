"""
lumen_tiktok.py — чистая механика TikTok-загрузчика (подписи, слайдшоу, детект видео-слайдов, выбор URL/качества, разбор ссылок на звук, MP3-теги, ffmpeg-пробинг, скачивание).

Вынесено из bot.py (аудит техдолга): только то, что не зовёт Telegram напрямую. Оркестрация handle_tiktok/handle_tiktok_sound остаётся в bot.py — неотделима от bot.send_*/_tg_call.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import socket
import tempfile
import time
import urllib.parse
from typing import Any

import aiohttp
from mutagen.id3 import APIC, ID3, TPE1, TIT2
from mutagen.mp3 import MP3

log = logging.getLogger("bot")


# ─────────────────── локализация подписи "оригинальный звук" ───────────────────
# TikWM даёт язык автора исходного видео, а подпись нужна на языке отправителя ссылки — берём message.from_user.language_code.
_ORIGINAL_SOUND_LABELS: dict[str, str] = {
    "ru": "Оригинальный звук",
    "uk": "Оригінальний звук",
    "be": "Арыгінальны гук",
    # Sentence case везде — поле идёт в MP3-тег как настоящее название трека.
    "en": "Original sound",
    "hi": "ओरिजिनल साउंड",
    "id": "Suara asli",
    "ms": "Bunyi asal",
    "vi": "Âm thanh gốc",
    "th": "เสียงต้นฉบับ",
    "fa": "صدای اصلی",
    "ur": "اصل آواز",
    "bn": "অরিজিনাল সাউন্ড",
    "fil": "Orihinal na tunog",
    "nl": "Origineel geluid",
    "zu": "Umsindo wokuqala",
    "pl": "Oryginalny dźwięk",
    "de": "Originalton",
    "es": "Sonido original",
    "fr": "Son original",
    "it": "Audio originale",
    "pt": "Som original",
    "tr": "Orijinal ses",
    "kk": "Түпнұсқа дыбыс",
    "uz": "Original tovush",
    "az": "Orijinal səs",
    "ka": "ორიგინალური ხმა",
    "hy": "Օրիգինալ ձայն",
    "ky": "Түпнуска үн",
    "ar": "الصوت الأصلي",
}
_ORIGINAL_SOUND_LABEL_DEFAULT = _ORIGINAL_SOUND_LABELS["en"]

# Фразы для детекта безымянного звука в bot.py: raw_title приходит на языке автора, а не получателя, поэтому проверяем все языки сразу. Строится из _ORIGINAL_SOUND_LABELS — единый источник правды.
_GENERIC_ORIGINAL_SOUND_PHRASES: tuple[str, ...] = tuple(sorted({v.lower() for v in _ORIGINAL_SOUND_LABELS.values()}))


def _original_sound_label(language_code: str | None, fallback_code: str | None = None) -> str:
    """Подпись на языке отправителя: первичный подтег до дефиса; нет кода — fallback_code чата, затем английский."""
    for code in (language_code, fallback_code):
        if not code:
            continue
        primary = code.split("-", 1)[0].strip().lower()
        if primary in _ORIGINAL_SOUND_LABELS:
            return _ORIGINAL_SOUND_LABELS[primary]
    return _ORIGINAL_SOUND_LABEL_DEFAULT


# ─────────────────── разбиение слайдшоу на группы sendMediaGroup ───────────────────

TELEGRAM_MEDIA_GROUP_CHUNK = 10


def _chunk_tiktok_media_items(items: list, chunk_size: int = TELEGRAM_MEDIA_GROUP_CHUNK) -> list[list]:
    """Режем на группы 2–10 (требование sendMediaGroup): хвост из 1 при 11/21/31 лечим займом у предпоследней."""
    if not items:
        return []
    chunks = [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]
    if len(chunks) >= 2 and len(chunks[-1]) == 1:
        borrowed = chunks[-2].pop()
        chunks[-1].insert(0, borrowed)
    return chunks


def _looks_like_video_bytes(data: bytes) -> bool:
    """Видео или картинка — по "ftyp" на смещении 4: Content-Type от TikWM недостоверен, поле в JSON не помечено."""
    return len(data) >= 12 and data[4:8] == b"ftyp"


def _slideshow_slide_urls(media_data: dict, images_to_fetch: list[str]) -> list[str]:
    """URL слайда: live_images[i] вместо images[i], если есть. play/hdplay у фото-постов — аудиодорожка, игнорируем."""
    live_images = media_data.get("live_images")
    if not isinstance(live_images, list):
        return images_to_fetch
    resolved: list[str] = []
    for idx, fallback_url in enumerate(images_to_fetch):
        live_url = live_images[idx] if idx < len(live_images) else None
        resolved.append(live_url if isinstance(live_url, str) and live_url.strip() else fallback_url)
    return resolved


def _tiktok_video_candidates(media_data: dict) -> list[dict[str, Any]]:
    """Кандидаты HD → обычное → с водяным (hdplay требует &hd=1 в запросе). Размер из ответа — пропускаем заведомо больше лимита до скачивания."""
    candidates: list[dict[str, Any]] = []
    for url_key, size_key, label in (
        ("hdplay", "hd_size", "HD"),
        ("play", "size", "стандартное"),
        ("wmplay", "wm_size", "с водяным знаком — резерв"),
    ):
        raw_url = media_data.get(url_key)
        if not raw_url:
            continue
        if not raw_url.startswith("http"):
            raw_url = "https://www.tikwm.com" + raw_url
        try:
            size_bytes = int(media_data.get(size_key) or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        candidates.append({"key": url_key, "url": raw_url, "size": size_bytes, "label": label})
    return candidates


# ── TikWM API: троттлинг + ретрай при 403 ──
# 11.08.2026, Sentry: оба зеркала ответили 403 с разрывом ~36мс — у TikWM лимит ~1 запр/сек.рвал 1.1с на процесс; 403 от обоих подряд — ретрай после паузы, а не "видео нет".
_TIKWM_MIN_INTERVAL_SEC = 1.1
_TIKWM_RETRY_BACKOFF_SEC = 2.0
_tikwm_last_request_ts: float | None = None
_tikwm_throttle_lock = asyncio.Lock()
# Точка подмены для тестов — реальные секунды ожидания не нужны ни одному тесту.
_sleep = asyncio.sleep


async def _tikwm_throttle() -> None:
    """Ждём интервал с прошлого запроса. None вместо 0.0 — чтобы первый вызов не решил, что "прошло мало времени"."""
    global _tikwm_last_request_ts
    async with _tikwm_throttle_lock:
        now = time.monotonic()
        if _tikwm_last_request_ts is not None:
            wait = _tikwm_last_request_ts + _TIKWM_MIN_INTERVAL_SEC - now
            if wait > 0:
                await _sleep(wait)
                now = time.monotonic()
        _tikwm_last_request_ts = now


async def _fetch_tikwm_media_data(
    session: aiohttp.ClientSession, resolved_url: str, headers: dict, *, hd: bool = True,
    proxy_base_url: str = "",
) -> dict | None:
    """Метаданные поста у TikWM (None — данных нет даже после ретрая). 200 с code != 0 — тоже не данные."""
    api_headers = {
        **headers,
        "Referer": "https://www.tikwm.com/",
        "Origin": "https://www.tikwm.com",
        "Accept": "application/json, text/plain, */*",
    }
    quoted = urllib.parse.quote(resolved_url)
    if proxy_base_url:
        suffix = f"/api/?url={quoted}&hd=1" if hd else f"/api/?url={quoted}"
        endpoints = [f"{proxy_base_url}{suffix}"]
    else:
        endpoints = [
            f"https://www.tikwm.com/api/?url={quoted}&hd=1" if hd else f"https://www.tikwm.com/api/?url={quoted}",
            f"https://tikwm.com/api/?url={quoted}&hd=1" if hd else f"https://tikwm.com/api/?url={quoted}",
        ]
    # True — только если ВСЕ попытки были "403 + пустое тело": узкий сигнал блокировки для диагн. лога.
    all_attempts_403_empty = True
    any_attempt_made = False
    for retry_round in range(2):
        saw_403 = False
        for api_url in endpoints:
            await _tikwm_throttle()
            any_attempt_made = True
            try:
                async with session.get(api_url, timeout=12, headers=api_headers) as r:
                    if r.status == 200:
                        all_attempts_403_empty = False
                        res = await r.json(content_type=None)
                        if res.get("code") == 0 and isinstance(res.get("data"), dict):
                            log.info("[tikwm] Successfully fetched media data from %s", api_url)
                            return res.get("data")
                        log.warning("[tikwm] Endpoint %s returned code %s: %s", api_url, res.get("code"), res.get("msg"))
                    else:
                        body = await r.read()
                        if r.status == 403:
                            saw_403 = True
                        if r.status != 403 or body:
                            all_attempts_403_empty = False
                        log.warning("[tikwm] Endpoint %s returned status %s: %r", api_url, r.status, body[:300])
            except Exception as e:
                all_attempts_403_empty = False
                log.warning("[tikwm] Request failed for endpoint %s: %s", api_url, e)
        if not saw_403 or retry_round == 1:
            break
        log.warning(
            '[tikwm] Both mirrors returned 403 in a row — looks like TikWM throttling (~1 request/sec) kicking in, not a genuinely unavailable video. Retrying in %.1fs.',
            _TIKWM_RETRY_BACKOFF_SEC,
        )
        await _sleep(_TIKWM_RETRY_BACKOFF_SEC)
    if any_attempt_made and all_attempts_403_empty:
        log.warning(
            "[tikwm][diag] ALL attempts (both mirrors, with throttling and a retry round) returned HTTP 403 with an EMPTY body — url=%s. The URL resolved correctly and requests were spaced out over time — this doesn't look like an ordinary transient error. Looks like this server's outbound IP is being blocked at a proxy/WAF level in front of TikWM (see README for a similar documented case with YouTube), which timing/URL tweaks on our side can't work around. To test the hypothesis: the same request to TikWM from a DIFFERENT IP (not HF Spaces).",
            resolved_url,
        )
    return None


class TikTokUserFacingError(RuntimeError):
    """Текст уже написан для пользователя: handle_tiktok показывает str(exc) as-is. Обычный RuntimeError — "сырой", пользователю не показывается."""


# ── Ссылка на страницу звука (не видео) ──
# TikWM не парсит /music/-ссылки ("Url parsing is failed") — ловим их здесь по ID.анных о звуке (bot-scoring для дата-центровых IP). Поэтому честно отвечаем сразу, рабочий путь — ссылка на видео с этим звуком.
_TIKTOK_MUSIC_PAGE_RE = re.compile(r"/music/\S*-(\d{6,})/?$", re.IGNORECASE)


def _tiktok_music_page_id(url: str) -> str | None:
    """Числовой ID из ссылки на страницу звука (признак "это звук, а не видео"), иначе None."""
    m = _TIKTOK_MUSIC_PAGE_RE.search(url)
    return m.group(1) if m else None


# ─────────────────── теги MP3 ───────────────────

def _write_mp3_tags(path: str, title: str, artist: str, cover: bytes | None) -> None:
    try:
        audio = MP3(path, ID3=ID3)
        with contextlib.suppress(Exception):
             audio.add_tags()
        audio.tags["TIT2"] = TIT2(encoding=3, text=title)
        audio.tags["TPE1"] = TPE1(encoding=3, text=artist)
        if cover:
            mime = "image/png" if cover.startswith(b"\x89PNG") else "image/jpeg"
            audio.tags["APIC"] = APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover)
        audio.save()
    except Exception as exc:
        log.warning("[tags] Failed to write tags: %s", exc)


# ── ffmpeg и скачивание бинарных URL ──
# Лимит размера (аудит 04.09.2026): слайды качаются до 35 параллельно — читаем потоково с капом.рвём свыше лимита.
TIKTOK_DOWNLOAD_MAX_BYTES = int(os.getenv("TIKTOK_DOWNLOAD_MAX_BYTES", str(75 * 1024 * 1024)))


# ── SSRF-гард для _download_url_bin ──
# Скомпрометированная выдача TikWM могла бы подсунуть внутренний адрес
# (localhost, metadata-IP облака 169.254.169.254 и т.п.) — сервер сам сходил бы
# внутрь своей сети. Литералы проверяем без DNS, хосты — через резолв
# (fail-closed: не резолвится — не качаем). Известное ограничение: TOCTOU между
# резолвом и коннектом (DNS-rebinding) лечится только на уровне коннектора;
# для нашей угрозы (прямой внутренний адрес в JSON) предпроверки достаточно.
def _host_resolves_to_public(host: str | None) -> bool:
    if not host:
        return False
    host = host.strip().strip("[]").lower()
    if host == "localhost":
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return False
    addrs = {info[4][0] for info in infos}
    if not addrs:
        return False
    try:
        return all(ipaddress.ip_address(a).is_global for a in addrs)
    except ValueError:
        return False


async def _download_url_bin(session: aiohttp.ClientSession, url: str, headers: dict | None = None) -> bytes | None:
    # URL — из JSON чужого сервиса (TikWM): качаем только http(s) (AUD-D-003)
    # и только с публичных адресов (SSRF-гард ниже).
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        log.warning("[download] Refusing non-HTTP(S) URL (scheme=%r).", scheme)
        return None
    if not _host_resolves_to_public(urllib.parse.urlsplit(url).hostname):
        log.warning("[download] Refusing non-public host for URL %r.", url)
        return None
    if headers is None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
        }
    try:
        async with session.get(url, headers=headers, timeout=60) as resp:
            if resp.status != 200:
                return None
            # Редирект мог увести на внутренний адрес уже после предпроверки —
            # сверяем конечный хост тоже. У тестовых заглушек .url нет — их пропускаем.
            final_url = getattr(resp, "url", None)
            final_host = urllib.parse.urlsplit(str(final_url)).hostname if final_url is not None else None
            if final_host is not None and not _host_resolves_to_public(final_host):
                log.warning("[download] Refusing redirect to non-public host %r.", final_host)
                return None
            # Content-Length — быстрый отказ ДО скачивания (сервер может соврать/не прислать — ниже та же проверка потоково по факту).
            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > TIKTOK_DOWNLOAD_MAX_BYTES:
                        log.warning("[download] Refusing to download %s: Content-Length %s exceeds the %d byte cap.", url, content_length, TIKTOK_DOWNLOAD_MAX_BYTES)
                        return None
                except ValueError:
                    pass
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.content.iter_chunked(65536):
                total += len(chunk)
                if total > TIKTOK_DOWNLOAD_MAX_BYTES:
                    log.warning("[download] Aborting download of %s: exceeded the %d byte cap mid-stream.", url, TIKTOK_DOWNLOAD_MAX_BYTES)
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except Exception as e:
        log.warning("[download] Failed to download URL: %s", e)
    return None


async def _communicate_process(proc: asyncio.subprocess.Process, *, timeout: float):
    async def finish():
        try:
            return await proc.communicate()
        finally:
            await proc.wait()

    completion = asyncio.create_task(finish())
    try:
        return await asyncio.wait_for(asyncio.shield(completion), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        while not completion.done():
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        with contextlib.suppress(Exception, asyncio.CancelledError):
            completion.result()
        raise


async def _probe_audio_duration(path: str) -> int:
    """Длительность аудиофайла в секундах (для send_audio — без неё Telegram показывает 0:00). 0 при любой неудаче."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await _communicate_process(proc, timeout=15)
        raw = stdout.decode(errors="replace").strip().splitlines()
        if raw and raw[0] and raw[0] != "N/A":
            return max(1, round(float(raw[0])))
    except Exception as e:
        log.warning("[ffmpeg] Audio probe failed: %s", e)
    return 0


async def _probe_video_dimensions(path: str) -> tuple[int, int, int]:
    """Возвращает (duration, width, height): без них Telegram показывает видео "сырым файлом" 0:00 (TikTok не всегда ставит faststart)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "default=noprint_wrappers=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await _communicate_process(proc, timeout=15)
        width = height = 0
        duration = 0
        for line in stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("width="):
                width = int(float(line.split("=", 1)[1] or 0))
            elif line.startswith("height="):
                height = int(float(line.split("=", 1)[1] or 0))
            elif line.startswith("duration="):
                raw = line.split("=", 1)[1]
                if raw and raw != "N/A":
                    duration = max(1, round(float(raw)))
        return duration, width, height
    except Exception as e:
        log.warning("[ffmpeg] Video probe failed: %s", e)
        return 0, 0, 0


async def _generate_video_thumbnail(path: str, duration: int) -> bytes | None:
    """Достаёт один кадр из видео как JPEG-превью для Telegram."""
    seek_at = min(1.0, max(0.0, duration / 2)) if duration else 0.5
    try:
        with tempfile.TemporaryDirectory() as tdir:
            thumb_path = os.path.join(tdir, "thumb.jpg")
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-ss", str(seek_at), "-i", path,
                "-frames:v", "1", "-vf", "scale=320:-1", thumb_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await _communicate_process(proc, timeout=15)
            if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                with open(thumb_path, "rb") as f:
                    return f.read()
    except Exception as e:
        log.warning("[ffmpeg] Thumbnail generation failed: %s", e)
    return None


async def _probe_and_thumbnail_from_path(path: str) -> tuple[int, int, int, bytes | None]:
    """Запускает ffprobe и ffmpeg параллельно: thumbnail зависит от duration лишь как min(1.0, duration/2)."""
    duration, width, height, thumb_bytes = 0, 0, 0, None
    try:
        (duration, width, height), thumb_bytes = await asyncio.gather(
            _probe_video_dimensions(path), _generate_video_thumbnail(path, 0),
        )
    except Exception as probe_exc:
        log.warning("[tiktok] Video metadata probe failed, sending without: %s", probe_exc)
    return duration, width, height, thumb_bytes


async def _probe_and_thumbnail_from_bytes(video_bytes: bytes) -> tuple[int, int, int, bytes | None]:
    """То же для байтов видео в памяти (видео-слайды): ffprobe/ffmpeg умеют только файлы — пишем во временный файл."""
    try:
        with tempfile.TemporaryDirectory() as tdir:
            raw_path = os.path.join(tdir, "slide.mp4")
            with open(raw_path, "wb") as f:
                f.write(video_bytes)
            return await _probe_and_thumbnail_from_path(raw_path)
    except Exception as probe_exc:
        log.warning("[tiktok] Video-slide metadata probe failed, sending without: %s", probe_exc)
        return 0, 0, 0, None


# ── Проверка "короткая ссылка довелась до канонического адреса поста" ──
# 12.08.2026, Sentry: HEAD отдал URL с пустым юзернеймом — требуем непустой сегмент @user.м строгий regex с непустым юзернеймом.
_RESOLVED_TIKTOK_POST_RE = re.compile(r"tiktok\.com/@[^/\s]+/(?:video|photo)/\d+", re.IGNORECASE)


def _looks_like_resolved_tiktok_url(url: str) -> bool:
    """True — канонический адрес поста (видео/фото) с непустым юзернеймом; иначе вызывающий код пробует резолвить дальше."""
    return bool(url) and bool(_RESOLVED_TIKTOK_POST_RE.search(url))


async def _resolve_tiktok_short(session: aiohttp.ClientSession, url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Upgrade-Insecure-Requests": "1"
    }
    try:
        async with session.head(url, allow_redirects=True, timeout=8, headers=headers) as resp:
             if resp.status < 400:
                  resolved = str(resp.url)
                  if _looks_like_resolved_tiktok_url(resolved):
                       return resolved
    except Exception as e:
         log.warning("[tiktok] HEAD resolution failed: %s", e)

    try:
        async with session.get(url, allow_redirects=True, timeout=10, headers=headers) as resp:
             resolved = str(resp.url)
             if not _looks_like_resolved_tiktok_url(resolved):
                   # Битый URL после GET тоже логируем — иначе остаётся только малопонятный 403 уже на стороне TikWM.
                  log.warning(
                       "[tiktok] Resolving short link %s didn't yield anything that looks like a post URL via either HEAD or GET (result: %s) — passing it through as-is, TikWM may reject it.", url, resolved,
                  )
             return resolved
    except Exception as e:
         log.warning("[tiktok] GET resolution failed: %s", e)

    return url
