"""lumen_tiktok.py handles TikTok downloader mechanics without direct Telegram: captions,
slideshows, URL/quality selection, sound-link parsing, MP3 tags, and ffmpeg probing.
Send orchestration is in lumen_tiktok_flow.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
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


# The caption must be in the language of the link sender, not the video author.
# TikWM provides the author's language, so use message.from_user.language_code.
_ORIGINAL_SOUND_LABELS: dict[str, str] = {
    "ru": "Оригинальный звук",
    "uk": "Оригінальний звук",
    "be": "Арыгінальны гук",
    # Sentence case throughout—the field goes into the MP3 tag as the actual track title.
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

# Phrases for detecting unnamed audio in bot.py: raw_title comes in the language of
# the author, not the recipient, so all languages are checked at once. Built from
# _ORIGINAL_SOUND_LABELS, the single source of truth.
_GENERIC_ORIGINAL_SOUND_PHRASES: tuple[str, ...] = tuple(sorted({v.lower() for v in _ORIGINAL_SOUND_LABELS.values()}))


def _original_sound_label(language_code: str | None, fallback_code: str | None = None) -> str:
    """Caption in the sender's language: the initial subtag before the hyphen; if there is no code, use the chat's fallback_code, then English."""
    for code in (language_code, fallback_code):
        if not code:
            continue
        primary = code.split("-", 1)[0].strip().lower()
        if primary in _ORIGINAL_SOUND_LABELS:
            return _ORIGINAL_SOUND_LABELS[primary]
    return _ORIGINAL_SOUND_LABEL_DEFAULT


# ─────────────────── splitting the slideshow into sendMediaGroup groups ───────────────────

TELEGRAM_MEDIA_GROUP_CHUNK = 10


def _chunk_tiktok_media_items(items: list, chunk_size: int = TELEGRAM_MEDIA_GROUP_CHUNK) -> list[list]:
    """Split into groups of 2–10 (sendMediaGroup requirement): fix a trailing single item
    at 11/21/31 by borrowing from the penultimate group."""
    if not items:
        return []
    chunks = [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]
    if len(chunks) >= 2 and len(chunks[-1]) == 1:
        borrowed = chunks[-2].pop()
        chunks[-1].insert(0, borrowed)
    return chunks


def _looks_like_video_bytes(data: bytes) -> bool:
    """Video or image, identified by \"ftyp\" at offset 4 (MP4) or the EBML signature at
    the start (WebM): the Content-Type from TikWM is unreliable, and the JSON field
    is unmarked."""
    return len(data) >= 12 and (data[4:8] == b"ftyp" or data[:4] == b"\x1a\x45\xdf\xa3")


def _slideshow_slide_urls(media_data: dict, images_to_fetch: list[str]) -> list[str]:
    """Slide URL: live_images[i] instead of images[i], if available. For photo posts, play/hdplay are the audio track; ignore them."""
    live_images = media_data.get("live_images")
    if not isinstance(live_images, list):
        return images_to_fetch
    resolved: list[str] = []
    for idx, fallback_url in enumerate(images_to_fetch):
        live_url = live_images[idx] if idx < len(live_images) else None
        resolved.append(live_url if isinstance(live_url, str) and live_url.strip() else fallback_url)
    return resolved


def _tiktok_video_candidates(media_data: dict) -> list[dict[str, Any]]:
    """Candidates: HD → normal → watermarked (hdplay requires &hd=1 in the request). Skip sizes reported in the response that clearly exceed the limit before downloading."""
    candidates: list[dict[str, Any]] = []
    for url_key, size_key, label in (
        ("hdplay", "hd_size", "HD"),
        ("play", "size", "стандартное"),
        ("wmplay", "wm_size", "с водяным знаком — резерв"),
    ):
        raw_url = media_data.get(url_key)
        if not raw_url:
            continue
        if not isinstance(raw_url, str):
            log.warning("[tiktok] Skipping non-string URL for %s (type=%s).", url_key, type(raw_url).__name__)
            continue
        if raw_url.startswith("//"):
            # Fix a protocol-relative URL to https; otherwise it would concatenate below into a broken "https://www.tikwm.com//host/path".
            raw_url = "https:" + raw_url
        elif not raw_url.startswith("http"):
            # Concatenation caused userinfo confusion ("@evil.com/x" became the host):
            # only a path relative to the base host.
            raw_url = urllib.parse.urljoin("https://www.tikwm.com/", raw_url)
        try:
            scheme = urllib.parse.urlsplit(raw_url).scheme.lower()
        except ValueError:
            # urlsplit fails on "]" in the host: skip one
            # a quality variant, not the entire post (audit 01.11.2026).
            log.warning("[tiktok] Skipping unparsable URL for %s.", url_key)
            continue
        if scheme not in ("http", "https"):
            log.warning("[tiktok] Skipping non-HTTP(S) URL for %s: %s.", url_key, _log_safe_url(raw_url))
            continue
        try:
            size_bytes = int(media_data.get(size_key) or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        candidates.append({"key": url_key, "url": raw_url, "size": size_bytes, "label": label})
    return candidates


# TikWM API: throttling + retry on 403
# 11.08.2026, Sentry: both mirrors returned 403 about 36 ms apart — TikWM has a
#     limit of about 1 request/sec, and one process ate 1.1 s; consecutive 403s from
#     both mean retry after a pause, not "video unavailable".
_TIKWM_MIN_INTERVAL_SEC = 1.1
_TIKWM_RETRY_BACKOFF_SEC = 2.0
_tikwm_last_request_ts: float | None = None
_tikwm_throttle_lock = asyncio.Lock()
# A substitution point for tests — no test needs actual seconds of waiting.
_sleep = asyncio.sleep


async def _tikwm_throttle() -> None:
    """Safe numeric parsing. None becomes 0.0 so a missing value does not break stats
    as \"unknown duration\"."""
    global _tikwm_last_request_ts
    async with _tikwm_throttle_lock:
        now = time.monotonic()
        if _tikwm_last_request_ts is not None:
            wait = _tikwm_last_request_ts + _TIKWM_MIN_INTERVAL_SEC - now
            if wait > 0:
                await _sleep(wait)
                now = time.monotonic()
        _tikwm_last_request_ts = now


# TikWM JSON must be small: use its own cap, not the general video limit.
TIKWM_JSON_MAX_BYTES = 1 * 1024 * 1024


def _log_safe_url(url: str) -> str:
    """Remove credentials, account paths, queries, and fragments before logging a URL."""
    try:
        parts = urllib.parse.urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return "<invalid-url>"
    if not hostname or "%" in hostname or parts.scheme.lower() not in ("http", "https"):
        return "<invalid-url>"
    try:
        host = ipaddress.ip_address(hostname).compressed
        authority = f"[{host}]" if ":" in host else host
    except ValueError:
        try:
            host = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return "<invalid-url>"
        labels = host.split(".")
        if len(host) > 253 or any(
            len(label) > 63 or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
            for label in labels
        ):
            return "<invalid-url>"
        authority = host
    if port is not None:
        authority = f"{authority}:{port}"
    # Media paths can contain signed identifiers, so retain no path at all.
    return urllib.parse.urlunsplit((parts.scheme.lower(), authority, "/<path>", "", ""))


def _safe_code_for_log(value: object) -> str:
    """Return only a numeric API code; upstream JSON fields are untrusted."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return f"<{type(value).__name__}>"


def _decimal_id(value: object) -> str:
    """Accept only ASCII decimal IDs used by TikTok and iTunes."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    text = str(value)
    return text if text.isascii() and text.isdecimal() else ""


async def _read_capped_body(resp: aiohttp.ClientResponse, url: str, *, cap_bytes: int | None = None) -> bytes | None:
    # TikWM JSON must be small: read it with the same cap as binary
    # download limit; otherwise a compromised proxy pours into memory without bound.
    cap = cap_bytes if cap_bytes is not None else TIKTOK_DOWNLOAD_MAX_BYTES
    content_length = resp.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > cap:
                log.warning("[tikwm] Refusing %s: Content-Length %s exceeds the %d byte cap.", _log_safe_url(url), content_length, cap)
                return None
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.content.iter_chunked(65536):
        total += len(chunk)
        if total > cap:
            log.warning("[tikwm] Aborting %s: exceeded the %d byte cap mid-stream.", _log_safe_url(url), cap)
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch_tikwm_media_data(
    session: aiohttp.ClientSession, resolved_url: str, headers: dict, *, hd: bool = True,
    proxy_base_url: str = "",
) -> dict | None:
    """Post metadata from TikWM (None means no data even after a retry). HTTP 200 with code != 0 also means no data."""
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
    # True only if ALL attempts returned "403 + empty body": a narrow blocking signal for the diagnostic log.
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
                        raw_body = await _read_capped_body(r, api_url, cap_bytes=TIKWM_JSON_MAX_BYTES)
                        if raw_body is None:
                            continue
                        try:
                            res = json.loads(raw_body.decode("utf-8"))
                        except Exception as e:
                            log.warning("[tikwm] Endpoint %s returned undecodable JSON (error=%s)", _log_safe_url(api_url), type(e).__name__)
                            continue
                        if res.get("code") == 0 and isinstance(res.get("data"), dict):
                            log.info("[tikwm] Successfully fetched media data from %s", _log_safe_url(api_url))
                            return res.get("data")
                        log.warning("[tikwm] Endpoint %s returned code %s", _log_safe_url(api_url), _safe_code_for_log(res.get("code")))
                    else:
                        raw_error = await _read_capped_body(r, api_url, cap_bytes=TIKWM_JSON_MAX_BYTES)
                        body = raw_error if raw_error is not None else b""
                        if r.status == 403:
                            saw_403 = True
                        if r.status != 403 or body:
                            all_attempts_403_empty = False
                        log.warning("[tikwm] Endpoint %s returned status %s (body_len=%d)", _log_safe_url(api_url), r.status, len(body))
            except Exception as e:
                all_attempts_403_empty = False
                log.warning("[tikwm] Request failed for endpoint %s (error=%s)", _log_safe_url(api_url), type(e).__name__)
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
            _log_safe_url(resolved_url),
        )
    return None


class TikTokUserFacingError(RuntimeError):
    """The text is already written for the user: handle_tiktok displays str(exc) as-is.
    An ordinary RuntimeError is "raw" and is not shown to the user.
    """


# ── Link to an audio page (not video) ──
# TikWM does not parse /music/ links ("Url parsing is failed"); catch them here by ID.
# Audio extraction is unreliable from data-center IPs, so report the limitation and point
# to a video link with sound as the working path.
_TIKTOK_MUSIC_PAGE_RE = re.compile(r"/music/\S*-(\d{6,})/?$", re.IGNORECASE)


def _tiktok_music_page_id(url: str) -> str | None:
    """Numeric ID from an audio-page link (the \"this is audio, not video\" signal), or None otherwise."""
    m = _TIKTOK_MUSIC_PAGE_RE.search(url)
    return m.group(1) if m else None


# ─────────────────── MP3 tags ───────────────────

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
        log.warning("[tags] Failed to write tags (error=%s)", type(exc).__name__)


# ── ffmpeg and downloading binary URLs ──
# Size limit (audit 04.09.2026): up to 35 slides download in parallel, so read streaming with a cap and cut anything above the limit.
# Number from the environment, via bot._env_number (garbage in the variable must
# not crash
# startup; deferred import because bot.py imports this module).
def _env_number_or_default(name: str, default: int) -> int:
    try:
        import bot
        # bot._env_number already casts and discards junk with the default; here only
        # a deferred import (bot.py imports this module) and a safeguard.
        return bot._env_number(name, default, cast=int, min_value=1024)
    except Exception:
        return default
TIKTOK_DOWNLOAD_MAX_BYTES = _env_number_or_default("TIKTOK_DOWNLOAD_MAX_BYTES", 75 * 1024 * 1024)


# ── SSRF guard for _download_url_bin ──
# A compromised TikWM response could supply an internal address
# (localhost, cloud metadata IP 169.254.169.254, etc.), so the server itself would
# inside its own network. Check literals without DNS, and hosts through resolution
# (fail-closed: if it does not resolve, do not download). Known limitation: TOCTOU between
# resolution and connection (DNS rebinding) can only be fixed at the connector level;
# for our threat (a direct internal address in JSON), the preliminary checks
# are sufficient.
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


def _resolve_result_is_public(result: Any) -> bool:
    """One resolution record is public: the name is already exposed in the IP, so check is_global."""
    try:
        return ipaddress.ip_address(result["host"]).is_global
    except (KeyError, TypeError, ValueError):
        return False


class _PublicOnlyResolver(aiohttp.abc.AbstractResolver):
    """Shared-session resolver: rejects non-public addresses on every resolution.

The _host_resolves_to_public precheck catches a direct private address in JSON,
but not the TOCTOU gap between resolution and connection (DNS rebinding: the first resolution
is public, the second private). The connector calls resolve before every new
connection, including redirect hops, so filtering here closes the entire
chain. We do not touch SNI/Host: only IPs are filtered; the hostname continues."""
    def __init__(self) -> None:
        # DefaultResolver requires a running loop in its constructor, so create
        # lazily on the first resolution, not here.
        self._inner: Any | None = None

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[Any]:
        if self._inner is None:
            self._inner = aiohttp.resolver.DefaultResolver()
        results = await self._inner.resolve(host, port=port, family=family)
        public = [entry for entry in results if _resolve_result_is_public(entry)]
        if not public and results:
            raise socket.gaierror(f"no public address for {host!r}")
        return public

    async def close(self) -> None:
        if self._inner is not None:
            await self._inner.close()
            self._inner = None


# Redirects are checked manually rather than trusted to the client: aiohttp with allow_redirects=True
# would go to the next hop before our final-host check. Every address
# from the Location chain must pass the same scheme and SSRF checks as the starting URL.
_DOWNLOAD_MAX_REDIRECTS = 5
_DOWNLOAD_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _checked_redirect_url(current_url: str, location: str | None) -> str | None:
    if not location:
        log.warning("[download] Redirect without a Location header for %s.", _log_safe_url(current_url))
        return None
    next_url = urllib.parse.urljoin(current_url, location)
    parts = urllib.parse.urlsplit(next_url)
    if parts.scheme.lower() not in ("http", "https"):
        log.warning("[download] Refusing redirect to non-HTTP(S) URL %s.", _log_safe_url(next_url))
        return None
    if not _host_resolves_to_public(parts.hostname):
        log.warning("[download] Refusing redirect to non-public host %s.", _log_safe_url(next_url))
        return None
    return next_url


async def _download_url_bin(session: aiohttp.ClientSession, url: str, headers: dict | None = None, *, cap_bytes: int | None = None) -> bytes | None:
    # URL from another service's JSON (TikWM): download only http(s) (AUD-D-003)
    # and only from public addresses (SSRF guard below). cap_bytes is a separate cap
    # the caller (video is trimmed by Telegram's send limit, not the general 75MB).
    # A non-string in place of a link previously crashed urlsplit and the entire post.
    if not isinstance(url, str):
        log.warning("[download] Refusing non-string URL (type=%s).", type(url).__name__)
        return None
    cap = cap_bytes if cap_bytes is not None else TIKTOK_DOWNLOAD_MAX_BYTES
    # Parsing happens inside the guard: urlsplit raises ValueError for an address with "]" in
    # the host ("https://p16]x.com/v.mp4"), and such links come from third-party JSON.
    # The same URL in a row indicates a bad retry, not a new track (production 01.11.2026).
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as parse_exc:
        log.warning("[download] Refusing unparsable URL (error=%s)", type(parse_exc).__name__)
        return None
    if parts.scheme.lower() not in ("http", "https"):
        log.warning("[download] Refusing non-HTTP(S) URL (scheme=<non-http-scheme>).")
        return None
    if not await asyncio.to_thread(_host_resolves_to_public, parts.hostname):
        # Do not write signed URLs (signature in the query) to the general log — see _log_safe_url.
        log.warning("[download] Refusing non-public host for URL %s.", _log_safe_url(url))
        return None
    if headers is None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
        }
    try:
        current_url = url
        for _ in range(_DOWNLOAD_MAX_REDIRECTS + 1):
            async with session.get(current_url, headers=headers, timeout=60, allow_redirects=False) as resp:
                if resp.status in _DOWNLOAD_REDIRECT_STATUSES:
                    # Hop resolution is also offloaded: it happens inside getaddrinfo, so we do not block the loop.
                    next_url = await asyncio.to_thread(_checked_redirect_url, current_url, resp.headers.get("Location"))
                    if next_url is None:
                        return None
                    current_url = next_url
                    continue
                if resp.status != 200:
                    return None
                # Test stubs may lack .url; a real response with
                # with author redirects disabled, this is the current URL, and each hop is already
                # checked above. Keep the check as the last line of defense.
                final_url = getattr(resp, "url", None)
                final_host = urllib.parse.urlsplit(str(final_url)).hostname if final_url is not None else None
                if final_host is not None and not await asyncio.to_thread(_host_resolves_to_public, final_host):
                    log.warning("[download] Refusing redirect to non-public host %r.", final_host)
                    return None
                # Content-Length allows an early rejection BEFORE downloading (the server may lie
                #     or omit it; the same check is performed on the stream below).
                content_length = resp.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) > cap:
                            log.warning("[download] Refusing to download %s: Content-Length %s exceeds the %d byte cap.", _log_safe_url(current_url), content_length, cap)
                            return None
                    except ValueError:
                        pass
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > cap:
                        log.warning("[download] Aborting download of %s: exceeded the %d byte cap mid-stream.", _log_safe_url(current_url), cap)
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        log.warning("[download] Too many redirects for %s.", _log_safe_url(url))
        return None
    except Exception as e:
        # Log the URL without its signature (see _log_safe_url): an aiohttp exception's text sometimes
        # contains the entire URL.
        log.warning("[download] Failed to download %s (error=%s)", _log_safe_url(url), type(e).__name__)
    return None


# How long to wait for a hung communicate after kill before removing the task (audit 06.10.2026).
_COMMUNICATE_DRAIN_SEC = 5.0


async def _communicate_process(proc: asyncio.subprocess.Process, *, timeout: float):
    async def finish():
        try:
            return await proc.communicate()
        finally:
            await proc.wait()

    completion = asyncio.create_task(finish())
    try:
        return await asyncio.wait_for(asyncio.shield(completion), timeout=timeout)
    except asyncio.TimeoutError:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        # Bounded drain: communicate, which survived kill, must not hold the loop forever.
        # A drain error propagates as-is (callers catch the broad
        # Exception); the original TimeoutError when drain simply ran out of time.
        try:
            await asyncio.wait_for(asyncio.shield(completion), timeout=_COMMUNICATE_DRAIN_SEC)
        except asyncio.TimeoutError:
            if completion.done():
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    completion.result()
            else:
                completion.cancel()
        raise
    except asyncio.CancelledError:
        # External cancellation is not draining: it must not be swallowed by continuing to wait.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        if completion.done():
            with contextlib.suppress(Exception, asyncio.CancelledError):
                completion.result()
        else:
            completion.cancel()
        raise


async def _probe_audio_duration(path: str) -> int:
    """Audio duration in seconds (without it, send_audio makes Telegram show 0:00). 0 on any failure."""
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
        log.warning("[ffmpeg] Audio probe failed (error=%s)", type(e).__name__)
    return 0


async def _probe_video_dimensions(path: str) -> tuple[int, int, int]:
    """Returns (duration, width, height): without them Telegram shows the video as a \"raw file\" at 0:00 (TikTok does not always set faststart)."""
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
                raw_w = line.split("=", 1)[1]
                # 'N/A' previously crashed float() and, with it, the valid duration from the entire probe.
                if raw_w and raw_w != "N/A":
                    width = int(float(raw_w))
            elif line.startswith("height="):
                raw_h = line.split("=", 1)[1]
                if raw_h and raw_h != "N/A":
                    height = int(float(raw_h))
            elif line.startswith("duration="):
                raw = line.split("=", 1)[1]
                if raw and raw != "N/A":
                    duration = max(1, round(float(raw)))
        return duration, width, height
    except Exception as e:
        log.warning("[ffmpeg] Video probe failed (error=%s)", type(e).__name__)
        return 0, 0, 0


async def _generate_video_thumbnail(path: str, duration: int) -> bytes | None:
    """Extracts one video frame as a JPEG preview for Telegram."""
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
        log.warning("[ffmpeg] Thumbnail generation failed (error=%s)", type(e).__name__)
    return None


async def _probe_and_thumbnail_from_path(path: str) -> tuple[int, int, int, bytes | None]:
    """Runs ffprobe and ffmpeg in parallel: thumbnail depends on duration only as min(1.0, duration/2)."""
    duration, width, height, thumb_bytes = 0, 0, 0, None
    try:
        (duration, width, height), thumb_bytes = await asyncio.gather(
            _probe_video_dimensions(path), _generate_video_thumbnail(path, 0),
        )
    except Exception as probe_exc:
        log.warning("[tiktok] Video metadata probe failed, sending without (error=%s)", type(probe_exc).__name__)
    return duration, width, height, thumb_bytes


def _write_file_sync(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


async def _probe_and_thumbnail_from_bytes(video_bytes: bytes) -> tuple[int, int, int, bytes | None]:
    """Same for video bytes in memory (video slides): ffprobe/ffmpeg only accept files — write to a temporary file."""
    try:
        with tempfile.TemporaryDirectory() as tdir:
            raw_path = os.path.join(tdir, "slide.mp4")
            # Writing up to 50 MB to the stream: in the loop it stalled everyone.
            await asyncio.to_thread(_write_file_sync, raw_path, video_bytes)
            return await _probe_and_thumbnail_from_path(raw_path)
    except Exception as probe_exc:
        log.warning("[tiktok] Video-slide metadata probe failed, sending without (error=%s)", type(probe_exc).__name__)
        return 0, 0, 0, None


# Check that "the short link resolved to the canonical post address"
# 12.08.2026, Sentry: HEAD returned a URL with an empty username — require a non-empty @user segment. A strict regex with a non-empty username.
_RESOLVED_TIKTOK_POST_RE = re.compile(r"tiktok\.com/@[^/\s]+/(?:video|photo)/\d+", re.IGNORECASE)


def _looks_like_resolved_tiktok_url(url: str) -> bool:
    """True means the canonical post address (video/photo) has a non-empty username; otherwise the caller tries to resolve it further."""
    return bool(url) and bool(_RESOLVED_TIKTOK_POST_RE.search(url))


async def _resolve_tiktok_short(session: aiohttp.ClientSession, url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Upgrade-Insecure-Requests": "1"
    }
    # The user's starting URL: strip the internal address down to the first request.
    if not await asyncio.to_thread(_host_resolves_to_public, urllib.parse.urlsplit(url).hostname):
        log.warning("[tiktok] Refusing to resolve non-public start URL %s.", _log_safe_url(url))
        return url
    # Parse redirects manually via _checked_redirect_url: aiohttp
    # with allow_redirects=True would visit the next hop before host validation.
    current_url = url
    for _ in range(_DOWNLOAD_MAX_REDIRECTS + 1):
        try:
            async with session.head(current_url, allow_redirects=False, timeout=8, headers=headers) as resp:
                if resp.status in _DOWNLOAD_REDIRECT_STATUSES:
                    next_url = await asyncio.to_thread(_checked_redirect_url, current_url, resp.headers.get("Location"))
                    if next_url is None:
                        break
                    current_url = next_url
                    continue
                if resp.status < 400:
                    resolved = str(resp.url)
                    if _looks_like_resolved_tiktok_url(resolved):
                        return resolved
                break
        except Exception as e:
            log.warning("[tiktok] HEAD resolution failed (error=%s)", type(e).__name__)
            break

    current_url = url
    try:
        for _ in range(_DOWNLOAD_MAX_REDIRECTS + 1):
            async with session.get(current_url, allow_redirects=False, timeout=10, headers=headers) as resp:
                if resp.status in _DOWNLOAD_REDIRECT_STATUSES:
                    next_url = await asyncio.to_thread(_checked_redirect_url, current_url, resp.headers.get("Location"))
                    if next_url is None:
                        break
                    current_url = next_url
                    continue
                # Deliberately do not read the body: only the final URL is needed, the connection
                # will simply close — no cap is needed, and no bytes are downloaded.
                resolved = str(resp.url)
                if not _looks_like_resolved_tiktok_url(resolved):
                    # Log malformed URLs after GET too; otherwise only TikWM's less informative 403 remains visible.
                    log.warning(
                        "[tiktok] Resolving short link %s didn't yield anything that looks like a post URL via either HEAD or GET (result: %s) — passing it through as-is, TikWM may reject it.", _log_safe_url(url), _log_safe_url(resolved),
                    )
                return resolved
    except Exception as e:
        log.warning("[tiktok] GET resolution failed (error=%s)", type(e).__name__)

    return url


# Exact track metadata via TikTok’s DSP identifier
# TikTok puts tt2dsp.SongId in the video page — the track ID in Apple Music. Using it
# the free iTunes Lookup API returns the real title, artist, and cover.
# This is the only available way to identify the track for licensed music:
# For such sounds, TikWM's music_info contains only "original sound - handle".
_DSP_TRACK_CACHE: dict[str, dict[str, str] | None] = {}
_DSP_TRACK_CACHE_MAX = 500


def _cache_dsp_track(sound_id: str, value: dict[str, str] | None) -> None:
    """Capped cache: without it, the dictionary grew until restart (review 17.11.2026)."""
    _DSP_TRACK_CACHE[sound_id] = value
    while len(_DSP_TRACK_CACHE) > _DSP_TRACK_CACHE_MAX:
        _DSP_TRACK_CACHE.pop(next(iter(_DSP_TRACK_CACHE)))
_DSP_DISABLED_UNTIL = 0.0
_DSP_PAGE_TIMEOUT_SEC = 6.0
_DSP_PAGE_UAS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
)


def _find_tt2dsp_song_id(obj: Any) -> str | None:
    """Finds SongId at any nesting level in a TikTok page's tt2dsp."""
    if isinstance(obj, dict):
        infos = obj.get("tt_to_dsp_song_infos")
        if isinstance(infos, list):
            for info in infos:
                if isinstance(info, dict) and info.get("SongId"):
                    return str(info["SongId"])
        for value in obj.values():
            found = _find_tt2dsp_song_id(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_tt2dsp_song_id(value)
            if found:
                return found
    return None


def _extract_tt2dsp_song_id(html: str) -> str | None:
    match = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">(.*?)</script>',
        html, re.S,
    )
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except Exception:
        return None
    return _find_tt2dsp_song_id(data)


async def _tiktok_dsp_song_id(session: aiohttp.ClientSession, video_id: str, unique_id: str) -> str | None:
    global _DSP_DISABLED_UNTIL
    if not video_id or not unique_id or time.monotonic() < _DSP_DISABLED_UNTIL:
        return None
    url = f"https://www.tiktok.com/@{urllib.parse.quote(unique_id.lstrip('@'))}/video/{video_id}?lang=en"
    hydration_seen = False
    for ua in _DSP_PAGE_UAS:
        try:
            async with session.get(
                url,
                headers={"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.google.com/"},
                timeout=aiohttp.ClientTimeout(total=_DSP_PAGE_TIMEOUT_SEC),
            ) as resp:
                if resp.status != 200:
                    continue
                # The TikTok page and the iTunes response are read with a cap, like TikWM:
                # without it, a compromised upstream can pour into memory without bound
                # (review 17.11.2026).
                raw_html = await _read_capped_body(resp, url, cap_bytes=2 * 1024 * 1024)
                if raw_html is None:
                    continue
                html = raw_html.decode("utf-8", errors="ignore")
            if '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"' in html:
                hydration_seen = True
            song_id = _extract_tt2dsp_song_id(html)
            if song_id:
                return song_id
        except Exception as exc:
            log.info("[tiktok-music] DSP page fetch failed (error=%s), trying next UA/fallback", type(exc).__name__)
    if hydration_seen:
        # The TikTok page is valid, but has no DSP track: this is an original
        # the sound. Do not disable DSP for 5 minutes for subsequent licensed
        # tracks (audit 29.10.2026); this sound_id is already cached as None.
        return None
    # TikTok returned no markup (anti-bot/network): do not hammer it on every download.
    _DSP_DISABLED_UNTIL = time.monotonic() + 300.0
    log.warning("[tiktok-music] DSP metadata unavailable for 5 minutes (TikTok page unreachable), using TikWM fields.")
    return None


async def _itunes_track(session: aiohttp.ClientSession, song_id: str) -> tuple[dict[str, str] | None, bool]:
    """(metadata indicating whether the failure was temporary). Do not cache temporary
    failures: otherwise, a single iTunes timeout would permanently deprive the track
    of exact fields (audit 16.11.2026)."""
    try:
        async with session.get(
            "https://itunes.apple.com/lookup",
            params={"id": song_id, "entity": "song"},
            timeout=aiohttp.ClientTimeout(total=6.0),
        ) as resp:
            if resp.status == 404:
                # The song is genuinely not found — this is the final response.
                return None, False
            if resp.status != 200:
                # 429/5xx — a temporary failure: do not cache the negative outcome.
                return None, True
            raw_json = await _read_capped_body(
                resp, "https://itunes.apple.com/lookup", cap_bytes=1024 * 1024)
            if raw_json is None:
                return None, True
            payload = json.loads(raw_json.decode("utf-8", errors="ignore"))
    except Exception as exc:
        log.info("[tiktok-music] iTunes lookup failed (error=%s)", type(exc).__name__)
        return None, True
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        return None, False
    track = results[0]
    title = (track.get("trackName") or "").strip()
    artist = (track.get("artistName") or "").strip()
    if not title or not artist:
        return None, False
    artwork = track.get("artworkUrl600") or track.get("artworkUrl100") or ""
    if artwork:
        artwork = re.sub(r"/\d+x\d+bb\.", "/600x600bb.", artwork)
    return {"title": title, "artist": artist, "cover": artwork}, False


async def _fetch_dsp_track_metadata(session: aiohttp.ClientSession, media_data: dict) -> dict[str, str] | None:
    """Exact metadata for a licensed track; None means keep the TikWM fields."""
    music_info = media_data.get("music_info") or {}
    sound_id = _decimal_id(music_info.get("id"))
    if sound_id and sound_id in _DSP_TRACK_CACHE:
        return _DSP_TRACK_CACHE[sound_id]
    video_id = str(media_data.get("id") or "")
    unique_id = (media_data.get("author") or {}).get("unique_id") or ""
    if not video_id or not unique_id:
        return None
    # Cache None only when the page was actually read and contains no DSP track.
    # Cooldown/unavailability is not a death sentence for the audio; otherwise one anti-bot would forever
    # sent the track to TikWM fields before restart (external audit 16.11.2026).
    was_paused = time.monotonic() < _DSP_DISABLED_UNTIL
    song_id = await _tiktok_dsp_song_id(session, video_id, unique_id)
    if not song_id:
        if sound_id and not was_paused and time.monotonic() >= _DSP_DISABLED_UNTIL:
            _cache_dsp_track(sound_id, None)
        return None
    track, transient = await _itunes_track(session, song_id)
    if sound_id and not transient:
        _cache_dsp_track(sound_id, track)
    if track:
        log.info(
            "[tiktok-music] DSP metadata: sound_id=%s song_id=%s title_len=%d artist_len=%d has_cover=%s",
            sound_id or "<invalid>", _decimal_id(song_id) or "<invalid>", len(track["title"]), len(track["artist"]), bool(track.get("cover")),
        )
    return track
