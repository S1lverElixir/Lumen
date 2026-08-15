FROM mwader/static-ffmpeg:8.1.2 AS ffmpeg

FROM python:3.10-slim

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
# Пакет ffmpeg в Debian trixie (текущий базовый слой python:3.10-slim) тянет ~205
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
