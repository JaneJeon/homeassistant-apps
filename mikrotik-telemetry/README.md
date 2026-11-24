# MikroTik Telemetry

Measure a MikroTik router from Home Assistant. The app pings the router once per second and reads WAN byte counters, CPU, memory and DHCP lease count through read-only SNMPv3. Optionally, it forwards RouterOS syslog received directly from the configured router to VictoriaLogs.

See [DOCS.md](DOCS.md) for options, scrape settings and metric queries.
