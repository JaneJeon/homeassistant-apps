# XB3 Telemetry

This app adds observations from the XB3 gateway to the existing Home Assistant
and VictoriaMetrics timeline. It does not change gateway settings or run ping,
traceroute, Wi-Fi scans, resets, or automatic recovery actions.

The existing wired HA probes show when the Green can still reach the gateway
while external targets lose replies. The new measurements show whether those
episodes coincide with modem errors, RF or channel-lock changes, Ethernet-link
changes, or valid uptime resets. Existing HA Router power and voltage readings
provide another comparison. Keep the raw packet-loss series, including 3%
episodes; the existing 20%-for-10-minutes flag does not gate collection.

Coinciding changes support an explanation. They do not uniquely distinguish
modem electronics, coax, drop/tap, or shared provider infrastructure. Steady
downstream readings also leave upstream reception and gateway forwarding
unresolved. The inspected portal exposes no CPU utilization, forwarding-drop
counters, or connection-table occupancy. Its gateway-origin ping allows only
four probes and is not a routine equivalent to HA's 30-probe batches.

## Connect the existing metrics and logs

1. Install this app and set `gateway_password` in its Options. The username
   defaults to `admin` and the gateway URL to `http://10.0.0.1`. The password stays
   in app Options; it is not printed, exported, or placed in this repository.
2. Add [example-scrape.yaml](example-scrape.yaml) to the VictoriaMetrics app's own
   config directory, which is `/config` inside that app. This is separate from
   HA Core's `/config`. Included files contain a job list directly, without a
   `scrape_configs:` wrapper. Check the target hostname against Supervisor: the example
   uses this repository's existing `894a6dc5` prefix.
3. Restart the VictoriaMetrics app from its Home Assistant app page to load the
   additional job. This briefly pauses metric ingestion. Check
   `up{job="xb3-telemetry"}` and `xb3_collection_success` before
   interpreting the modem values. `up` only confirms the exporter responded.
4. Vector currently ingests other HA app containers through journald. After
   installation, verify this app's own stream in VictoriaLogs using its
   `xb3_exporter_started` event. The log worker starts after one minute. Then
   look for `xb3_gateway_log_query` and `xb3_gateway_log_snapshot_record` event text.
   The latter appears only when a query returns records. Stdout output alone
   does not establish successful ingestion.
   The current Vector configuration can retain JSON as message text. No new log
   database or shipper is required.

The exporter listens on port 19090 inside the app network. Host/LAN port mapping
is disabled by default because VictoriaMetrics can use the app hostname. If
enabled for inspection, keep the listener on the existing LAN/tailnet boundary.
`/healthz` reports exporter process availability, not gateway or internet health.

Each Prometheus scrape runs one bounded sequence of passive reads of
`network_setup.php`, `hardware.php`, and `lan.php`. Concurrent scrape requests
share the same sequence. Collection has a 45-second budget; the example uses
a one-minute scrape interval and a 55-second scrape timeout. Cookies stay in
memory. Recognized session expiry permits one authentication recovery; rejected
authentication backs off rather than repeatedly submitting the password.
After a timeout or transport failure, remaining pages in that scrape are marked
unattempted. This limits additional same-session requests while earlier server
work may still run. It can omit otherwise available observations; collection
tries again on the next ordinary scrape.

| Measurement                 | Metric                                                                                                       | Meaning                                                                                        |
| --------------------------- | ------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| RF lock, frequency, power   | `xb3_channel_locked`, `xb3_channel_frequency_hertz`, `xb3_channel_power_dbmv`                                | Downstream receive and upstream transmit observations at the displayed RF slot                 |
| Downstream SNR              | `xb3_downstream_snr_db`                                                                                      | Gateway-reported receiver reading                                                              |
| Codewords                   | `xb3_downstream_codewords_total{slot,kind}`                                                                  | Raw cumulative unerrored, correctable, and uncorrectable totals at the displayed codeword slot |
| Ethernet                    | `xb3_lan_link_active`, `xb3_lan_link_speed_bits_per_second`                                                  | Four port states and available link rates; HA's exact port is not mapped                       |
| Uptime and displayed status | `xb3_uptime_seconds`, `xb3_internet_ui_active`                                                               | Valid positive uptime and a literal UI flag with the limits below                              |
| DRAM and flash              | `xb3_dram_reported_value`, `xb3_flash_reported_megabytes`                                                    | Reported resource values with the interpretation limits below                                  |
| Measurement quality         | `xb3_collection_success`, `xb3_collection_last_success_timestamp_seconds`, `xb3_page_fetch_duration_seconds` | Availability, age, and cost of each measurement group                                          |
| Log queries                 | `xb3_log_query_success`, `xb3_log_query_duration_seconds`, `xb3_log_records_returned`                        | Collection results by category and queried time window, not new event counts                   |

RF and codeword labels are UI positions, not actual DOCSIS channel IDs. The
portal fetches those tables separately; their physical-channel association is
not independently established. RF frequency is a separate gauge. Counter
decreases can be resets or rollover. Existing VictoriaMetrics should calculate
rates; exclude or visibly qualify windows with decreases or RF slot changes.
Absence of a decrease does not establish stable physical-channel identity.
Corrected codewords are recovered errors. Codeword error fractions are not IP
packet-loss percentages.

The actual portal sometimes returns valid RF tables alongside blank model,
clock, firmware, or hardware fields. Its templates can turn missing inputs into
zero uptime, zero flash capacity, or Disabled labels. This app keeps usable
groups and omits unavailable measurements. It does not refresh old readings with
new timestamps or substitute zero. Last-success timestamps remain available so
gaps can be distinguished from quiet measurements.

Collection success establishes a fetched and parsed observation. The portal
does not provide acquisition timestamps for every underlying measurement group,
so it cannot independently establish that the gateway backend refreshed them.

`xb3_internet_ui_active` means only that the UI displayed Active (1) or Inactive
(0). Missing backend data can produce Inactive. The inspected upstream code
forces Active in `bridge-static` mode. Neither value establishes external reachability. Its collection
success means the displayed flag was parsed, not that its backend was verified.

DRAM values remain raw with `reported_unit="MB",unit_verified="false"`. The
observed total was 1048576 with the UI displaying MB; a conversion is not
established for this firmware. The UI's DRAM Available field maps to upstream
`MemoryStatus.Free`, not established Linux `MemAvailable`. Neither a large used
value nor the static processor clock establishes hardware overload.

## Retrieved gateway logs

`log_mode` defaults to `firewall`, with a 900-second interval. `off` disables
queries. `all` additionally requests System and Event logs. On this gateway,
System and Event queries each took about 121 seconds and returned empty arrays;
a Firewall query took about 3.5 seconds and returned records. The fast category
was tested alongside metric reads in separate authenticated sessions. Isolation
of the slow categories has not been established, so they are not enabled by
default. A timeout stops that entire log poll because server work can continue
after client cancellation.

Log queries use a separate session, never overlap within this app, and stay out
of the metric scrape. Each poll requests Today and Yesterday to preserve records
across gateway midnight. Output records carry their category, actual query
window, query ID, index, collector UTC, raw gateway timestamp, and original
decoded row. Repeated polls and overlapping windows can retrieve the same row.
These are snapshots of retained records, not newly occurring drops at poll time.
The reported Count is not accumulated. An empty successful array, failed query,
and unavailable measurement remain distinct.

Gateway timestamps have no verified timezone. Valid network-page clock readings
are logged as `xb3_gateway_clock_snapshot` with collector UTC and fetch duration
to retain an observation pair. No automatic timezone correction is applied.
The observed Firewall records described IPv6 drops with placeholder endpoints;
they do not explain loss to HA's IPv4 probe addresses or establish that legitimate
browsing traffic was blocked. The browsing impairment and IPv4 loss are related
observations, not yet proven to be identical failing traffic.

## Local development and validation

Use Node 22.22.2 or newer. Supply `XB3_PASSWORD` through the existing secret
manager, then run these commands from `rootfs/opt/xb3-telemetry`:

```sh
npm ci --ignore-scripts
npm test
npm run build
npm run once
```

`--once` prints one Prometheus exposition. `--logs-once` retrieves the configured
log categories and prints JSON snapshots. The normal process serves `/metrics`.
`XB3_GATEWAY_URL`, `XB3_USERNAME`, `XB3_LOG_MODE`, `XB3_LOG_INTERVAL_SECONDS`,
`XB3_LISTEN_HOST`, `XB3_PORT`, and `XB3_OPTIONS_FILE` support local runs without
putting secrets into tracked files. Building the Docker image also runs the
TypeScript check and regression tests before producing the runtime image.

The fixtures include sanitized complete and partial HTML from the actual
TG1682P PHP interface. Tests also mutate those captures to exercise malformed
cells, unavailable groups, and false defaults. The implementation targets this
observed interface, not every gateway carrying the XB3 name.

Useful source references: [VictoriaMetrics extra scrape jobs](https://github.com/VictoriaMetrics-Community/homeassistant-addon-victoriametrics/blob/main/victoria-metrics/prometheus.tpl),
[upstream XB3 network rendering](https://github.com/rdkcentral/webui/blob/dd9ad14f97bec8dc94fc45045c9b86d3e0869b38/source/Styles/xb3/code/network_setup.php),
[hardware field mappings](https://github.com/rdkcentral/webui/blob/dd9ad14f97bec8dc94fc45045c9b86d3e0869b38/source/Styles/xb3/code/hardware.php),
and [log query handler](https://github.com/rdkcentral/webui/blob/dd9ad14f97bec8dc94fc45045c9b86d3e0869b38/source/Styles/xb3/code/actionHandler/ajax_troubleshooting_logs.php).
Published upstream code helps explain the interface; it does not establish the
deployed gateway's exact binaries or patch state.
