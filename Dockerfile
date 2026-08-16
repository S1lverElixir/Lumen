# ОБНОВЛЕНО (аудит зависимостей, 16 августа 2026): 8.1.2 -> 9.0 — мажорный
# апстрим-бамп FFmpeg (ABI-слом внутри библиотек, но не в CLI-флагах, которые
# реально использует бот: -c:a libopus/-vf scale/ffprobe -show_entries — ни один
# из них не тронут релиз-нотами 9.0, см. официальный Changelog). Проверено
# точечно перед бампом, а не вслепую.
FROM mwader/static-ffmpeg:9.0 AS ffmpeg

# ОБНОВЛЕНО (аудит зависимостей, 16 августа 2026): 3.10 -> 3.13 — requirements.txt
# и так уже требовали только "Python 3.10+" (не конкретно 3.10), все закреплённые
# версии зависимостей (aiogram 3.30, google-genai 2.18, fastapi 0.141, uvicorn
# 0.52 и т.д.) заявляют поддержку вплоть до 3.14 — полный прогон тестового сьюта
# (320 тестов) под новой версией прошёл без единого изменения кода.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ca-certificates нужен для TLS из Python (Gemini API, OpenRouter, Pollinations, TikWM,
# Deno-прокси и т.д.). apt-get install ffmpeg отсюда убран — см. ниже.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ffmpeg/ffprobe — статические бинарники из отдельного минимального образа, а не из apt.
# Пакет ffmpeg в Debian trixie (текущий базовый слой python:3.13-slim) тянет ~205
# транзитивных зависимостей — X11, Vulkan, Mesa OpenGL, PulseAudio, JACK, libsdl2,
# Samba/Kerberos, шрифты и т.п. (GUI/аудиосервер-стек, нужный только для ffplay,
# который в проекте не используется вообще). Это лишние ~460 МБ в образе и заметно
# более долгая и "тяжёлая" сборка — а боту нужны только сами бинарники ffmpeg/ffprobe
# для headless-задач (конвертация аудио в OGG/Opus для /tts, извлечение превью и
# метаданных видео для TikTok). Статические бинарники не имеют внешних зависимостей
# вообще — они просто копируются в PATH.
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /app/requirements.txt

COPY . /app

EXPOSE 7860

CMD ["python", "-u", "bot.py"]
