# syntax=docker/dockerfile:1
# ha-mcp source is pinned to v7.2.0 (peeled commit) and checked by SHA-256.
FROM ghcr.io/astral-sh/uv:0.11.0-python3.13-trixie-slim@sha256:5b216b72b3bc10f983f82b39b5386455bfa08d2139afb4cb3f6c9f060484ea5d AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

RUN python3 - <<'PY'
import hashlib
import io
import tarfile
import urllib.request

commit = "651adae00bd9386fa0d609698a8be8ce153aba73"
expected = "655ed1c4c2d4d6557ee9e07fa8f132ae1fe7d8d94a743d482bf99ec73c015f01"
url = f"https://codeload.github.com/homeassistant-ai/ha-mcp/tar.gz/{commit}"
with urllib.request.urlopen(url) as response:
    archive = response.read()
actual = hashlib.sha256(archive).hexdigest()
if actual != expected:
    raise SystemExit(f"Source checksum mismatch: expected {expected}, received {actual}")
with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
    members = source.getmembers()
    prefix = members[0].name.split("/", 1)[0] + "/"
    for member in members:
        if member.name.startswith(prefix):
            member.name = member.name[len(prefix):]
            if member.name:
                source.extract(member, "/app", filter="data")
PY

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.13-slim@sha256:3de9a8d7aedbb7984dc18f2dff178a7850f16c1ae7c34ba9d7ecc23d0755e35f

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY rootfs/start.py /start.py
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["python3", "/start.py"]

LABEL \
    org.opencontainers.image.title="Home Assistant MCP" \
    org.opencontainers.image.description="ha-mcp 7.2.0 served over Streamable HTTP" \
    org.opencontainers.image.source="https://github.com/JaneJeon/homeassistant-apps" \
    org.opencontainers.image.licenses="MIT"
