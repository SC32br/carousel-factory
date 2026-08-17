# Фабрика каруселей: Python 3.11 + Chromium (вёрстка слайдов) + ffmpeg (живая обложка).
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHROME_BIN=/usr/bin/chromium \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        ffmpeg \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-noto-core \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY project/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY project/ /app/
# В docker compose код ещё и монтируется томом, чтобы править без пересборки.

CMD ["python", "-u", "botd.py"]
