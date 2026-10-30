FROM alpine:3.22
ARG TARGETARCH
RUN apk add --no-cache python3 ca-certificates curl
RUN case "$TARGETARCH" in amd64) sha=1b495bde563825cf83dc7c0425a9d8fa03e7214858e0949f9177efc6bb1f8bfc ;; arm64) sha=3d5f965b7a75f713742a7f37195c1d845f47d9dc9d096a2cce29e693f08fd641 ;; *) exit 1 ;; esac && \
    curl -fsSL "https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/v1.153.0/victoria-metrics-linux-${TARGETARCH}-v1.153.0.tar.gz" -o /tmp/release.tar.gz && \
    echo "$sha  /tmp/release.tar.gz" | sha256sum -c - && tar -xzf /tmp/release.tar.gz -C / && rm /tmp/release.tar.gz
RUN case "$TARGETARCH" in amd64) sha=85aea24a4829cf26033d810aceb7888070bd4eaff321357168ff6db24bf7c00c ;; arm64) sha=8153c4feb73564215950a299c803d5c1e974772cd34b8c162d43ea2c5b516df5 ;; *) exit 1 ;; esac && \
    curl -fsSL "https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/v1.153.0/vmutils-linux-${TARGETARCH}-v1.153.0.tar.gz" -o /tmp/release.tar.gz && \
    echo "$sha  /tmp/release.tar.gz" | sha256sum -c - && tar -xzf /tmp/release.tar.gz -C / && rm /tmp/release.tar.gz
COPY native-backup-common/native_backup.py /usr/local/bin/native-backup
COPY native-backup-common/start.py /usr/local/bin/start-victoria
COPY native-backup-common/backup-pre /backup-pre
ENV VICTORIA_ENGINE=victoriametrics
CMD ["/usr/local/bin/start-victoria"]
