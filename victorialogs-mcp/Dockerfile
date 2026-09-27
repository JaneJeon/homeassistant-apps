# syntax=docker/dockerfile:1
FROM golang:1.26.2-alpine@sha256:f85330846cde1e57ca9ec309382da3b8e6ae3ab943d2739500e08c86393a21b1 AS builder
ARG SOURCE_COMMIT=0baa4f48c532d64162c88bad939d00f6e6524649
RUN apk add --no-cache git ca-certificates nodejs npm
WORKDIR /src
RUN git init . \
    && git remote add origin https://github.com/VictoriaMetrics/mcp-victorialogs.git \
    && git fetch --depth 1 origin "${SOURCE_COMMIT}" \
    && git checkout --detach FETCH_HEAD
RUN cd web && npm ci && npm run build
RUN go mod download && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/mcp-victorialogs ./cmd/mcp-victorialogs

FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
ARG BASHIO_VERSION=0.17.5
RUN apk add --no-cache bash ca-certificates tini curl jq \
    && mkdir -p /usr/lib/bashio \
    && curl -L -f -s "https://github.com/hassio-addons/bashio/archive/v${BASHIO_VERSION}.tar.gz" \
    | tar -xzf - --strip 1 -C /usr/lib/bashio \
    && ln -s /usr/lib/bashio/lib/bashio /usr/bin/bashio
COPY --from=builder /out/mcp-victorialogs /usr/local/bin/mcp-victorialogs
COPY rootfs /
EXPOSE 18082
ENTRYPOINT ["/sbin/tini", "--", "/entrypoint.sh"]
LABEL org.opencontainers.image.source="https://github.com/JaneJeon/homeassistant-apps"
