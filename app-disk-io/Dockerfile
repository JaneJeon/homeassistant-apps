# syntax=docker/dockerfile:1
FROM golang:1.25-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends libsystemd-dev pkg-config && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY rootfs/collector/ ./
RUN go test ./... && CGO_ENABLED=1 go build -trimpath -ldflags='-s -w' -o /app-disk-io .

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libsystemd0 util-linux && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app-disk-io /usr/local/bin/app-disk-io
COPY --chmod=0755 rootfs/entrypoint.sh /usr/local/bin/app-disk-io-entrypoint
ENTRYPOINT ["/usr/local/bin/app-disk-io-entrypoint"]
LABEL org.opencontainers.image.source="https://github.com/JaneJeon/homeassistant-apps"
