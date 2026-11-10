# syntax=docker/dockerfile:1
FROM golang:1.26.2-alpine@sha256:f85330846cde1e57ca9ec309382da3b8e6ae3ab943d2739500e08c86393a21b1 AS builder
ARG SOURCE_COMMIT=4f00299f1130835ab81969dd953ed6d26906c675
RUN apk add --no-cache git ca-certificates nodejs npm
WORKDIR /src
RUN git init . \
    && git remote add origin https://github.com/VictoriaMetrics/mcp-victoriametrics.git \
    && git fetch --depth 1 origin "${SOURCE_COMMIT}" \
    && git checkout --detach FETCH_HEAD
RUN cd web && npm ci && npm run build
RUN go mod download && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/mcp-victoriametrics ./cmd/mcp-victoriametrics

FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
ARG BASHIO_VERSION=0.17.5
RUN apk add --no-cache bash ca-certificates tini curl jq \
    && mkdir -p /usr/lib/bashio \
    && curl -L -f -s "https://github.com/hassio-addons/bashio/archive/v${BASHIO_VERSION}.tar.gz" \
    | tar -xzf - --strip 1 -C /usr/lib/bashio \
    && ln -s /usr/lib/bashio/lib/bashio /usr/bin/bashio
COPY --from=builder /out/mcp-victoriametrics /usr/local/bin/mcp-victoriametrics
COPY rootfs /
EXPOSE 18081
ENTRYPOINT ["/sbin/tini", "--", "/entrypoint.sh"]
LABEL org.opencontainers.image.source="https://github.com/JaneJeon/homeassistant-apps"
