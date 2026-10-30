FROM alpine:3.22
ARG TARGETARCH
RUN apk add --no-cache python3 ca-certificates curl
RUN case "$TARGETARCH" in amd64) sha=d14f585144b8d6813f15e11f0041f487e15e10e5f5e5a31be0311367e93d3494 ;; arm64) sha=91338c3e5e3d743a862c0a8665bf80862f639dbd4de6f6ff19ada7df5e9acf45 ;; *) exit 1 ;; esac && \
    curl -fsSL "https://github.com/VictoriaMetrics/VictoriaLogs/releases/download/v1.52.0/victoria-logs-linux-${TARGETARCH}-v1.52.0.tar.gz" -o /tmp/release.tar.gz && \
    echo "$sha  /tmp/release.tar.gz" | sha256sum -c - && tar -xzf /tmp/release.tar.gz -C / && rm /tmp/release.tar.gz
COPY native-backup-common/native_backup.py /usr/local/bin/native-backup
COPY native-backup-common/start.py /usr/local/bin/start-victoria
COPY native-backup-common/backup-pre /backup-pre
ENV VICTORIA_ENGINE=victorialogs
CMD ["/usr/local/bin/start-victoria"]
