# syntax=docker/dockerfile:1
FROM alpine:3.22
RUN apk add --no-cache iperf3
USER nobody
EXPOSE 5201/tcp 5201/udp
ENTRYPOINT ["iperf3", "-s"]
LABEL \
    org.opencontainers.image.title="Home Assistant App: iperf3 server" \
    org.opencontainers.image.description="iperf3 server for measuring LAN and Wi-Fi throughput" \
    org.opencontainers.image.source="https://github.com/JaneJeon/homeassistant-apps"
