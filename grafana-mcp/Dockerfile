# syntax=docker/dockerfile:1
FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8

ARG BASHIO_VERSION=0.17.5
ARG MCP_GRAFANA_VERSION=1.6.0
ARG TARGETARCH

RUN apk add --no-cache bash ca-certificates curl jq tini \
    && mkdir -p /usr/lib/bashio \
    && curl -fsSL "https://github.com/hassio-addons/bashio/archive/v${BASHIO_VERSION}.tar.gz" \
    | tar -xzf - --strip 1 -C /usr/lib/bashio \
    && ln -s /usr/lib/bashio/lib/bashio /usr/bin/bashio \
    && case "${TARGETARCH}" in \
        amd64) archive="mcp-grafana_Linux_x86_64.tar.gz"; checksum="f53c710d4de85b7515e24cbc193173468dad9dcccc5f3d6b2ffe9ad68eefe3c7" ;; \
        arm64) archive="mcp-grafana_Linux_arm64.tar.gz"; checksum="17c39971c306514334b64cd3837cfdaf259537a18386725af06056c3778a966f" ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/grafana/mcp-grafana/releases/download/v${MCP_GRAFANA_VERSION}/${archive}" -o /tmp/mcp-grafana.tar.gz \
    && echo "${checksum}  /tmp/mcp-grafana.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/mcp-grafana.tar.gz -C /usr/local/bin mcp-grafana \
    && chmod 0755 /usr/local/bin/mcp-grafana \
    && rm -f /tmp/mcp-grafana.tar.gz

COPY rootfs /

EXPOSE 8000
ENTRYPOINT ["/sbin/tini", "--", "/entrypoint.sh"]

LABEL \
    org.opencontainers.image.title="Home Assistant App: Grafana MCP" \
    org.opencontainers.image.description="Grafana MCP server using Streamable HTTP" \
    org.opencontainers.image.source="https://github.com/JaneJeon/homeassistant-apps" \
    org.opencontainers.image.licenses="Apache-2.0"
