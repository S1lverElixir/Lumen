"""Живой зонд генерации картинок: что провайдеры отвечают на те же запросы, что идёт из бота.

Нужен потому, что моки в тестах не видят главного: Pollinations игнорирует model и
nologo, а Cloudflare может отказать настройкой или дневным бюджетом. Ключи только из
окружения или .env, значения нигде не печатаются.

Запуск:
    python eval/image_probe.py              # оба провайдера по одному разу
    python eval/image_probe.py --repeat     # Pollinations дважды одним промптом (повторяемость)
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lumen_images  # noqa: E402

PROMPT = "a red fox portrait, cinematic light"


def _env(name: str, default: str = "") -> str:
    """Окружение важнее файла; .env читаем молча и только нужные имена."""
    value = os.getenv(name, "").strip()
    if value:
        return value
    env_file = ROOT / ".env"
    if not env_file.exists():
        return default
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, val = line.partition("=")
        if sep and key.strip() == name:
            return val.strip().strip('"').strip("'")
    return default


def _digest(body: bytes) -> str:
    """Короткий отпечаток ответа: формат по байтам + размер, для проверки повтором."""
    ext = lumen_images.image_filename(body) or "не-картинка"
    return f"{ext} {len(body)} Б"


def _meta(body: bytes) -> str:
    """Метаданные Pollinations встроены в JPEG — по ним видно, какую модель и seed
    сервис применил на самом деле, и включил ли знак."""
    match = re.search(rb'"prompt":.{0,200}', body)
    return match.group().decode("utf-8", "replace")[:200] if match else ""


async def _probe_pollinations(session: aiohttp.ClientSession, *, twice: bool) -> None:
    print("== Pollinations (каталог) ==")
    async with session.get("https://image.pollinations.ai/models", timeout=aiohttp.ClientTimeout(total=15)) as resp:
        print(f"  GET /models -> HTTP {resp.status}: {(await resp.text())[:200]}")

    async def one(tag: str) -> None:
        start = time.monotonic()
        try:
            body = await lumen_images._pollinations_text_to_image(session, PROMPT, timeout_sec=90.0)
            print(f"  {tag}: {_digest(body)} за {time.monotonic() - start:.1f}s | {_meta(body)}")
        except Exception as exc:
            print(f"  {tag}: {type(exc).__name__}: {exc} за {time.monotonic() - start:.1f}s")

    await one("генерация")
    if twice:
        print("  (жду 47с лимита 1 картинка/45с на IP)")
        await asyncio.sleep(47)
        await one("тот же промпт")


async def _probe_cloudflare(session: aiohttp.ClientSession) -> None:
    print("== Cloudflare Workers AI ==")
    account = _env("CLOUDFLARE_ACCOUNT_ID")
    token = _env("CLOUDFLARE_API_TOKEN")
    if not account or not token:
        print("  пропущено: нет CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN")
        return
    print(f"  модель: {lumen_images.CLOUDFLARE_IMAGE_MODEL}, аккаунт настроен")
    start = time.monotonic()
    try:
        body = await lumen_images._cloudflare_text_to_image(session, PROMPT, timeout_sec=90.0)
        print(f"  генерация: {_digest(body)} за {time.monotonic() - start:.1f}s")
    except Exception as exc:
        print(f"  генерация: {type(exc).__name__}: {exc} за {time.monotonic() - start:.1f}s")


async def main() -> int:
    twice = "--repeat" in sys.argv
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        await _probe_cloudflare(session)
        await _probe_pollinations(session, twice=twice)
    print("\nПромпт зонда фиксированный, приватных данных нет. Ответы провайдеров — в stdout.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
