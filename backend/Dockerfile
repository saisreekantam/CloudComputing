# One Dockerfile, two targets: `gateway` (API only) and `worker` (pipeline: ffmpeg + yt-dlp + deno).
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

FROM base AS gateway
COPY notetaker ./notetaker
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=4s --retries=6 CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/healthz', timeout=3)" || exit 1
CMD ["uvicorn", "notetaker.gateway.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

FROM base AS worker
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# yt-dlp needs a JavaScript runtime to solve YouTube's player challenges.
COPY --from=denoland/deno:bin-2.1.4 /deno /usr/local/bin/deno
COPY requirements-worker.txt .
RUN pip install -r requirements-worker.txt && pip install -U "yt-dlp[default]"
# Bake the speech-to-text model into the image so the fallback never downloads at job time.
ARG ASR_MODEL=base
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${ASR_MODEL}', device='cpu', compute_type='int8')"
ENV HF_HUB_OFFLINE=1
COPY notetaker ./notetaker
# SIGTERM → graceful drain (see worker/main.py); compose gives it 40s.
STOPSIGNAL SIGTERM
CMD ["python", "-m", "notetaker.worker.main"]
