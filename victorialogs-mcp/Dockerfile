# syntax=docker/dockerfile:1
FROM golang:1.27.0-alpine@sha256:4c9fe60190a2a3350ddc51de80d0224b8a6698d12bdfc999fee45ea9d6c46dbc AS builder
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
