# MikroTik Telemetry

Set `router_host` to the router's LAN IPv4 address and `wan_interface` to its internet-facing RouterOS interface. The default WAN name is `ether1`.

Create a read-only RouterOS SNMPv3 community restricted to the Home Assistant host. Use HMAC-SHA1 authentication and AES privacy. Set `snmp_username`, `snmp_auth_key` and `snmp_privacy_key` in the app options. Home Assistant Supervisor supports `!secret` references for the keys.

The app serves Prometheus metrics on port `19100`. Add a scrape job for `894a6dc5-mikrotik-telemetry:19100` from a VictoriaMetrics app installed from this repository. A different repository ID changes the hostname prefix.

```yaml
- job_name: mikrotik-telemetry
  scrape_interval: 15s
  static_configs:
    - targets: [894a6dc5-mikrotik-telemetry:19100]
```

To send measurements to an existing VictoriaMetrics app, set `victoriametrics_url` to its internal base URL, for example `http://894a6dc5-victoria-metrics:8428`. Each poll then writes freshly sampled metrics to the Prometheus import endpoint with the job name `mikrotik-telemetry`. Every reading in one write carries the same timestamp, so readings from one poll can be matched exactly. Select one collection path per app instance to avoid duplicate series.

## Wi-Fi client traffic

Leave `wifi_clients` empty to skip client polling. The router's own Wi-Fi interfaces are read either way. To measure a client, enter a stable name and the MAC address the client uses on this SSID. The address may be a device's private Wi-Fi address. Use a unique name and MAC for each of up to 16 clients. Names use letters, digits, underscores, or hyphens and start with a letter or digit.

```yaml
wifi_clients:
  - name: example-client
    mac: "02:00:00:00:00:01"
```

The collector reads the RouterOS `/interface wifi registration-table` SNMP MIB with the configured read-only SNMPv3 credentials. It looks up configured stations only. Metric labels contain the configured `client` name and the radio interface's IF-MIB `if_index`. The address is used for lookup and does not appear in metrics or logs. The app omits missing association rows and leaves WAN and resource polling independent of this table.

| Signal | Query |
| --- | --- |
| Station → AP bits per second by client | `8 * sum by (client) (rate(mikrotik_wifi_client_receive_bytes_total[5m]))` |
| AP → station bits per second by client | `8 * sum by (client) (rate(mikrotik_wifi_client_transmit_bytes_total[5m]))` |
| Station → AP bytes observed in 24 hours | `sum by (client) (increase(mikrotik_wifi_client_receive_bytes_total[24h]))` |
| AP → station bytes observed in 24 hours | `sum by (client) (increase(mikrotik_wifi_client_transmit_bytes_total[24h]))` |
| Signal strength by client | `mikrotik_wifi_client_signal_dbm` |
| AP → station link rate by client | `mikrotik_wifi_client_tx_rate_bits_per_second` |
| Station → AP link rate by client | `mikrotik_wifi_client_rx_rate_bits_per_second` |
| Band by client | `mikrotik_wifi_client_band_info` (the `band` label, value 1) |
| Time associated by client | `mikrotik_wifi_client_uptime_seconds` |
| Router Wi-Fi interfaces | `mikrotik_wifi_interface_info` (labels `interface`, `ssid`, `channel`, `index`) |
| Router Wi-Fi interface frequency | `mikrotik_wifi_interface_frequency_mhz` |

The byte counters are radio-side counts for each association, including traffic to other LAN devices. Each series starts at zero and accumulates deltas between observed polls. The first poll after a missing row, failed Wi-Fi poll, lower association uptime, or decreased counter establishes a new baseline. The collector retains the prior total when a row reappears and resets its totals on restart. A reconnect between two otherwise increasing samples can go undetected. A changed interface index produces a new series. Apply `rate` or `increase` before summing across indices. The query engine can smooth or extrapolate samples around gaps. An unavailable row does not establish zero traffic, and the measured totals can omit traffic during gaps and resets.

The signal, link-rate, band and time-associated gauges come from the same per-client read as the byte counters. They exist only while the client is associated: a client missing from the registration table, or a failed Wi-Fi poll, leaves no sample. An absent series means no current reading (not associated, or the poll failed), never a stale value. Signal and link rates are exported as RouterOS reports them; the MIB does not state their units. On a live hAP ax3 with RouterOS 7.24.7, signal is dBm and the link rates are bits per second. The association uptime is typed as TimeTicks, but RouterOS counts whole seconds in it, so it is exported unchanged.

The interface gauges come from a walk of the router's own interface table (`mtxrWifiInterfaces`), which lists radios, not stations. `channel` is empty on RouterOS versions whose MIB lacks the current-channel column, and when the interface is not running. The frequency gauge appears only when the router reports a plain number. If the walk fails, the app logs one warning, omits the interface series, and keeps polling clients and the WAN.

At startup and then at most once an hour, the app logs the raw values read for each associated client (registration-table columns 4, 6, 8, 9 and 10, by client name) and the raw interface-table rows. These lines confirm the column mapping against the live router.

## RouterOS logs to VictoriaLogs

Set `victorialogs_url` to the internal base URL of a VictoriaLogs app, for example `http://894a6dc5-victoria-logs:9428`, to enable UDP syslog input on port `5514`. The listener accepts datagrams only from `router_host`; configure that router to send BSD syslog to the Home Assistant host on UDP port 5514. Leave the option empty to keep the UDP listener disabled.

Logs are sent directly to VictoriaLogs in bounded batches. Each record has the app receipt time in `timestamp`, RouterOS's original timestamp text in `routeros_timestamp`, a sanitized `message`, decoded `syslog_priority`, `syslog_facility`, and `syslog_severity`, plus stream fields `source="routeros"` and `router` (the RouterOS hostname), and the sender address in `router_ip`. The app does not write raw router events to stdout, preventing duplicate ingestion by the host log collector. On a full queue or failed batch, the affected records are dropped rather than retried without bound.

## Read the signals

| Signal | Query or metric |
| --- | --- |
| Download bits per second | `8 * rate(mikrotik_wan_receive_octets_total[1m])` |
| Upload bits per second | `8 * rate(mikrotik_wan_transmit_octets_total[1m])` |
| Download bytes over 24 hours | `increase(mikrotik_wan_receive_octets_total[24h])` |
| Upload bytes over 24 hours | `increase(mikrotik_wan_transmit_octets_total[24h])` |
| Router ping outcomes | `mikrotik_ping_outcomes_total{outcome="reply"}` and `{outcome="no_reply_by_deadline"}` |
| Router resource usage | `mikrotik_cpu_load_percent` and `mikrotik_memory_bytes` |
| IPv4 DHCP lease count | `mikrotik_dhcp_leases` |
| Router uptime | `mikrotik_uptime_seconds` |
| SNMP poll status | `mikrotik_snmp_up` and `mikrotik_snmp_last_success_timestamp_seconds` |
| RouterOS log ingestion | `mikrotik_routeros_logs_accepted_total`, `mikrotik_routeros_logs_forwarded_total`, and `mikrotik_routeros_logs_dropped_total` |

WAN totals cover all bytes the selected interface counts. Use the stored counter history to measure a period and account for observed resets. A reboot can lose bytes sent between the last sample and the reset. The DHCP lease count measures leases rather than current device activity.

Use `mikrotik_snmp_up` to identify unavailable router data. A successful push records in VictoriaMetrics even when the router poll fails, so an omitted WAN sample is distinct from a measured zero.

The router ping and the XB3 Telemetry app observe different devices. Compare router replies with the modem's page readings and the external probes when investigating a stall.
