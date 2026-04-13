# https://developers.home-assistant.io/docs/apps/configuration#app-dockerfile
FROM grafana/grafana-image-renderer:v5.8.1

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends jq \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy root filesystem
COPY rootfs /

RUN chmod +x /run.sh

USER 65532

ENTRYPOINT ["/run.sh"]

LABEL \
    org.opencontainers.image.title="Home Assistant App: Grafana Image Renderer" \
    org.opencontainers.image.description="Grafana Image Renderer service for rendering Grafana panels and dashboards as images" \
    org.opencontainers.image.source="https://github.com/JaneJeon/homeassistant-apps" \
    org.opencontainers.image.licenses="Apache License 2.0"
