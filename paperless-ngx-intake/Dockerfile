# syntax=docker/dockerfile:1
FROM python:3.13-slim@sha256:3de9a8d7aedbb7984dc18f2dff178a7850f16c1ae7c34ba9d7ecc23d0755e35f

# tzdata gives the Mail intake page local times. The invisible text layer needs no font package:
# its glyphless font is built into the app (docai/glyphless.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY rootfs/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY rootfs/app /app
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["python3", "-m", "paperless_intake"]

LABEL \
    org.opencontainers.image.title="Paperless-ngx Scanner" \
    org.opencontainers.image.description="Turns scans upright and into reading order with Google OCR, then commits them into Paperless-ngx" \
    org.opencontainers.image.source="https://github.com/JaneJeon/homeassistant-apps" \
    org.opencontainers.image.licenses="MIT"
