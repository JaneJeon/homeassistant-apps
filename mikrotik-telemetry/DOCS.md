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

To send measurements to an existing VictoriaMetrics app, set `victoriametrics_url` to its internal base URL, for example `http://894a6dc5-victoria-metrics:8428`. Each poll then writes freshly sampled metrics to the Prometheus import endpoint with the job name `mikrotik-telemetry`. Select one collection path per app instance to avoid duplicate series.

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
| SNMP poll status | `mikrotik_snmp_up` and `mikrotik_snmp_last_success_timestamp_seconds` |

WAN totals cover all bytes the selected interface counts. Use the stored counter history to measure a period and account for observed resets. A reboot can lose bytes sent between the last sample and the reset. The DHCP lease count measures leases rather than current device activity.

Use `mikrotik_snmp_up` to identify unavailable router data. A successful push records in VictoriaMetrics even when the router poll fails, so an omitted WAN sample is distinct from a measured zero.

The router ping and the XB3 Telemetry app observe different devices. Compare router replies with the modem's page readings and the external probes when investigating a stall.
