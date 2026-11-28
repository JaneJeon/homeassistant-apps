# MikroTik Telemetry

Measure a MikroTik router from Home Assistant. The app pings the router once per second and reads WAN byte counters, CPU, memory and DHCP lease count through read-only SNMPv3. It can also measure per-client radio traffic and link state for an explicit Wi-Fi allowlist, list the router's Wi-Fi interfaces, and forward RouterOS syslog to VictoriaLogs.

See [DOCS.md](DOCS.md) for options, scrape settings and metric queries.
