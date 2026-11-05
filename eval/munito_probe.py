"""Живой зонд Munito AI: доступность моделей, лимиты, время до первого токена.

Ключ только из окружения или .env; значение нигде не печатается.
Запуск: python eval/munito_probe.py
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "https://api.munito.ai/inference/v1"

# Порядок = гипотеза головы цепочки; 1017 есть только в EU/глобальном хосте.
PROBE_MODELS = [
    "deepseek-ai/deepseek-v4.1-flash-1017",
    "zai-org/glm-5.3-flash",
    "deepseek-ai/deepseek-v4-flash",
    "qwen/qwen3.8-27b",
]

_SYSTEM = "Ты — Lumen, внимательный ассистент. Отвечай по-русски, коротко и по делу."
_USER = "Что такое контекстное окно у языковой модели? Одно предложение."


def _env(name: str, default: str = "") -> str:
    """Окружение важнее файла; .env читаем молча и только нужные имена."""
    value = os.getenv(name, "").strip()
    if value:
        return value
    env_file = ROOT / ".env"
    if not env_file.exists():
        return default
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip().lstrip("\ufeff")
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def _rate_headers(headers) -> dict:
    return {k: v for k, v in headers.items() if "ratelimit" in k.lower() or k.lower() == "munito-region"}


async def _list_models(session: aiohttp.ClientSession, base: str, key: str) -> None:
    async with session.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}) as resp:
        data = await resp.json(content_type=None)
        ids = [m.get("id") for m in (data.get("data") or data.get("models") or []) if isinstance(m, dict)]
        print(f"HTTP {resp.status} · моделей в каталоге: {len(ids)}")
        print("  " + ", ".join(str(i) for i in ids))
        print("  region:", resp.headers.get("munito-region"), "· limits:", _rate_headers(resp.headers))


async def _once(session: aiohttp.ClientSession, base: str, key: str, model: str, *, stream: bool) -> None:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": _USER}],
        "max_tokens": 400,
        "stream": stream,
    }
    t0 = time.monotonic()
    async with session.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body,
        timeout=aiohttp.ClientTimeout(total=90.0, connect=15.0),
    ) as resp:
        if resp.status >= 400:
            raw = await resp.text()
            print(f"  {model} stream={stream} → HTTP {resp.status}: {raw[:200]}")
            print(f"    limits: {_rate_headers(resp.headers)}")
            return
        if not stream:
            data = await resp.json(content_type=None)
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            usage = data.get("usage") or {}
            print(
                f"  {model} stream=False → {(time.monotonic() - t0):.2f}s · "
                f"токены in/out {usage.get('prompt_tokens')}/{usage.get('completion_tokens')} · "
                f"ответ: {text[:90]!r}"
            )
            print(f"    limits: {_rate_headers(resp.headers)}")
            return
        first = None
        chunks = []
        async for raw_line in resp.content:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                piece = json.loads(payload)
            except json.JSONDecodeError:
                continue
            delta = ((piece.get("choices") or [{}])[0].get("delta") or {}).get("content")
            if delta:
                if first is None:
                    first = time.monotonic() - t0
                chunks.append(delta)
        print(
            f"  {model} stream=True → первый токен {(first or -1):.2f}s, "
            f"всего {(time.monotonic() - t0):.2f}s, символов {sum(len(c) for c in chunks)}"
        )


async def main() -> None:
    key = _env("MUNITO_API_KEY")
    base = _env("MUNITO_BASE_URL", DEFAULT_BASE).rstrip("/")
    print(f"{base} · {'ключ из окружения/.env' if key else 'НЕТ КЛЮЧА — выхожу'}")
    print(f"ключ: {'замаскирован, длина ' + str(len(key)) if key else '—'}")
    if not key:
        return
    conn = aiohttp.TCPConnector(limit=4)
    async with aiohttp.ClientSession(connector=conn) as session:
        await _list_models(session, base, key)
        for model in PROBE_MODELS:
            print(f"\n=== {model} ===")
            await _once(session, base, key, model, stream=False)
            await _once(session, base, key, model, stream=True)
        # Пакет из 6 запросов подряд: на котором приходит 429/503 — тот и есть реальный RPM.
        print("\n=== burst 6 одинаковых запросов (голова списка) ===")
        burst_model = PROBE_MODELS[0]
        for i in range(6):
            t0 = time.monotonic()
            await _once(session, base, key, burst_model, stream=False)
            print(f"    #{i + 1} {(time.monotonic() - t0):.2f}s")


if __name__ == "__main__":
    # Windows консоль в cp1251 рвёт кириллицу и стрелки (прод замер 05.11.2026).
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
