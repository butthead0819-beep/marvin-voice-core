FROM python:3.12-slim

# System deps:
#   libopus0     — Opus audio decoding (discord.py voice receive)
#   ffmpeg       — audio transcoding (TTS playback, audio processing)
#   libsodium23  — Discord voice encryption
#   gcc / build  — compile some Python C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopus0 \
    ffmpeg \
    libsodium23 \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install core voice pipeline package first (marvin_voice_core/)
COPY pyproject.toml requirements-core.txt ./
COPY marvin_voice_core/ ./marvin_voice_core/
RUN pip install --no-cache-dir -e . -r requirements-core.txt

# Install bot-level deps (LLM, TTS, music — excludes macOS-only packages)
RUN pip install --no-cache-dir \
    python-dotenv \
    google-genai \
    google-generativeai \
    groq \
    edge-tts \
    yt-dlp \
    aiohttp \
    aiofiles \
    openai \
    syncedlyrics \
    davey==0.1.5 \
    duckduckgo-search

# Copy the rest of the project
COPY . .

# Linux-specific defaults:
#   VISION_ENABLED=false — screen capture requires a display (no X11 in Docker)
#   PYTHONUNBUFFERED=1   — route print() to Docker logs instead of file buffer
ENV VISION_ENABLED=false
ENV PYTHONUNBUFFERED=1

# Swift STT will fail on Linux (expected) and automatically fall back to
# Faster-Whisper. No flag needed — stt_handler.py handles this gracefully.

# .env must be bind-mounted at runtime:
#   docker run --env-file .env ...
# Never bake secrets into the image.

CMD ["python", "main_discord.py"]
