# syntax=docker/dockerfile:1
FROM grafana/grafana:13.1.0-24374455864

# Home Assistant baseline:
# https://developers.home-assistant.io/docs/apps/configuration#app-dockerfile
ARG BASHIO_VERSION=0.17.5

USER root

# bashio
RUN apk add --no-cache jq curl \
    && mkdir -p /usr/lib/bashio \
    && curl -L -f -s "https://github.com/hassio-addons/bashio/archive/v${BASHIO_VERSION}.tar.gz" \
    | tar -xzf - --strip 1 -C /usr/lib/bashio \
    && ln -s /usr/lib/bashio/lib/bashio /usr/bin/bashio

# Copy root filesystem
COPY rootfs /

# Switch back to Grafana
USER grafana
ENTRYPOINT ["tini", "--", "/entrypoint.sh"]

LABEL \
    org.opencontainers.image.title="Home Assistant App: Grafana" \
    org.opencontainers.image.description="No-Bullshit Grafana Add-On" \
    org.opencontainers.image.source="https://github.com/JaneJeon/homeassistant-apps" \
    org.opencontainers.image.licenses="Apache License 2.0"
