"""eval/provider_caps_probe.py — живой замер возможностей Munito по параметрам.

Зачем: карта _MUNITO_CAPS («gemma-4-12b и pplx-decider не принимают
reasoning_effort, головы принимают, tools принимают все текстовые») до этого
файла опиралась на разовые вызовы, которых в репозитории не было. Теперь
утверждение можно повторить одной командой, и оно датировано.

    python eval/provider_caps_probe.py

Ключи берутся из окружения или из .env основного дерева (LUMEN_ENV_DIR);
значения нигде не печатаются. Ответ на каждый вопрос — один токен.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.getenv("MUNITO_BASE_URL", "").strip() or "https://api.munito.ai/inference/v1"
ENV_FILE = Path(os.getenv("LUMEN_ENV_DIR", r"C:/Users/egork/Documents/Default Project/Lumen")) / ".env"

# Модель × параметр. Список сверен с _MUNITO_ORDER и _MUNITO_CAPS: снимать
# признак возможности можно только по живому ответу провайдера.
MODELS = (
    "deepseek-ai/deepseek-v4.1-flash-1017",
    "zai-org/glm-5.3-flash",
    "deepseek-ai/deepseek-v4-flash",
    "google/gemma-4-12b",
    "perplexity-ai/pplx-decider-v1.2",
)
PARAMS = ("reasoning_effort", "tools")


def key() -> str:
    value = (os.getenv("MUNITO_API_KEY", "") or "").strip()
    if value:
        return value
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip().lstrip("﻿")
            if line.startswith("MUNITO_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def post(token: str, body: dict, timeout: float = 25.0) -> tuple[int, str]:
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")[:400]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")[:400]
    except Exception as exc:  # сеть, таймаут, битый JSON
        return 0, exc.__class__.__name__


def base_body(model: str) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Скажи одно слово: ок"}],
        "max_tokens": 8,
        "stream": False,
    }


def tool_payload() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск в интернете",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                           "required": ["query"]},
        },
    }


def main() -> int:
    token = key()
    if not token:
        print("НЕТ КЛЮЧА MUNITO_API_KEY — выхожу")
        return 1
    print(f"Замер возможностей Munito, endpoint {BASE}")
    rows = []
    for model in MODELS:
        for param in PARAMS:
            body = base_body(model)
            if param == "reasoning_effort":
                body["reasoning_effort"] = "medium"
            else:
                body["tools"] = [tool_payload()]
                body["tool_choice"] = "auto"
            started = time.monotonic()
            status, detail = post(token, body)
            secs = time.monotonic() - started
            accepted = status == 200
            hint = "" if accepted else detail.replace("\n", " ")[:120]
            rows.append((model, param, accepted, status, round(secs, 2), hint))
            print(f"  {model:42s} {param:16s} {'принято' if accepted else 'ОТКАЗ'}"
                  f" HTTP {status}  {secs:.2f}s {hint}")
    print("\nИтог для _MUNITO_CAPS:")
    for model in MODELS:
        accepts = {param for m, param, ok, *_ in rows if m == model and ok}
        print(f"  {model:42s} " + ", ".join(f"{p}={p in accepts}" for p in PARAMS))
    print(f"\nОбращений к провайдеру: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
