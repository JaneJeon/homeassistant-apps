# syntax=docker/dockerfile:1
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS builder

ARG SOURCE_COMMIT=4ba74570e6161b955fd1f3071ce9472c5b689295
ARG SOURCE_SHA256=f2300678103013a8dd2ffc44c67e892c186c41a05249bf7b7b74e616892b5771

RUN apk add --no-cache ca-certificates curl patch
WORKDIR /src
RUN curl -fsSL "https://codeload.github.com/nloui/paperless-mcp/tar.gz/${SOURCE_COMMIT}" -o /tmp/source.tar.gz \
    && echo "${SOURCE_SHA256}  /tmp/source.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/source.tar.gz --strip-components=1 -C /src \
    && rm /tmp/source.tar.gz

COPY rootfs/upstream.patch /tmp/upstream.patch
COPY rootfs/package-lock.json /src/package-lock.json
RUN patch -p1 < /tmp/upstream.patch \
    && npm ci \
    && npm run build \
    && npm prune --omit=dev

FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293

RUN apk add --no-cache jq tini
WORKDIR /app
COPY --from=builder /src/build /app/build
COPY --from=builder /src/node_modules /app/node_modules
COPY --from=builder /src/package.json /app/package.json
COPY rootfs/run.sh /run.sh
RUN chmod 0755 /run.sh

EXPOSE 3000
ENTRYPOINT ["/sbin/tini", "--", "/run.sh"]

LABEL \
    org.opencontainers.image.title="Home Assistant App: Paperless-ngx MCP" \
    org.opencontainers.image.description="Read-only Paperless-ngx MCP over Streamable HTTP" \
    org.opencontainers.image.source="https://github.com/JaneJeon/homeassistant-apps" \
    org.opencontainers.image.licenses="ISC"
