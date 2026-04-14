# syntax=docker/dockerfile:1
# https://developers.home-assistant.io/docs/apps/configuration#app-dockerfile
FROM grafana/grafana-image-renderer:v5.8.1

ARG BASHIO_VERSION=0.17.5

USER root

# bashio
RUN apt-get update \
    && apt-get install -y --no-install-recommends jq curl \
    && mkdir -p /usr/lib/bashio \
    && curl -L -f -s "https://github.com/hassio-addons/bashio/archive/v${BASHIO_VERSION}.tar.gz" \
    | tar -xzf - --strip 1 -C /usr/lib/bashio \
    && ln -s /usr/lib/bashio/lib/bashio /usr/bin/bashio \
    && rm -rf /var/lib/apt/lists/*

# Copy root filesystem
COPY rootfs /

# Switch back to Grafana image renderer
USER 65532
ENTRYPOINT ["tini", "--", "/run.sh"]
CMD ["server"]

LABEL \
    org.opencontainers.image.title="Home Assistant App: Grafana Image Renderer" \
    org.opencontainers.image.description="Grafana Image Renderer service for rendering Grafana panels and dashboards as images" \
    org.opencontainers.image.source="https://github.com/JaneJeon/homeassistant-apps" \
    org.opencontainers.image.licenses="Apache License 2.0"
