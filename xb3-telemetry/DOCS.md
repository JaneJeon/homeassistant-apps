# XB3 Telemetry

This app records individual IPv4 probe outcomes and passive observations from
the ARRIS TG1682P admin portal. VictoriaMetrics stores counters, gauges and RTT
histograms. The existing Vector → VictoriaLogs path stores raw probe outcomes,
gateway snapshots and retrieval brackets. It also runs scheduled and requested
download/upload speed tests against Cloudflare. It changes no gateway settings or
recovery automations.

## Timing and what can be concluded

Each of the existing targets—gateway `10.0.0.1`, Google `8.8.8.8` and Cloudflare
`1.1.1.1`—has one scheduled probe per second. Unanswered probes do not postpone
the next slot. At most two subprocesses per target can be outstanding. Missed
local slots are recorded rather than filled with catch-up traffic.

Every outcome retains a target, sequence, scheduled UTC and completion UTC.
Attempted probes also retain their recorded launch attempt, monotonic bounds,
scheduler lag, clock-offset evidence and utility-reported RTT where available.
Skipped local slots have no launch or RTT. Launch and completion are process bounds;
the utility does not expose kernel transmit/receive timestamps. A recognized
one-packet timeout is `no_reply_by_deadline`. Permission errors, parent process
timeouts, malformed results and local scheduling skips remain separate.

The app uses its own container network namespace on HA. Its interface addresses
are logged at startup. Equivalence with HA Core's routing and the post-NAT source
address are not established. ICMP observations do not establish identical
behavior for browsing traffic or IPv6. One-second sampling can still miss a
shorter event between probes.

Gateway reads run independently. The network page has a configurable 20- or
60-second schedule; hardware and LAN are due every 60 seconds. A shared gate
serializes all portal access, including authentication, metric pages and log
queries. Metric ticks skip explicitly as `portal_busy` while logs hold the gate
or wait their turn. Log polling waits abortably and holds the gate through its
complete Today/Yesterday sequence. There is no catch-up burst. A timeout or
transport failure skips later pages and imposes at least 60 seconds of shared
cooldown before either worker can issue another portal request. A skipped tick
preserves the prior snapshot and its original age. Slow opt-in System/Event log
queries can therefore create visible gaps in gateway observations. Probes and
HTTP scrapes continue independently of the gate.
The 20-second option should be checked against actual request duration, usable
observations and probe scheduling on the particular gateway.

HTML parsing runs in a persistent worker so synchronous DOM work does not run
on the probe scheduler's JavaScript thread. Worker requests are bounded; parsing
duration includes startup and message overhead. HTTP completion bounds are
captured before parsing, and parse failure remains an unavailable observation.

`/metrics` serves memory immediately. A five-second scrape does not issue five-
second portal requests. Cached page values keep their original timestamps and
page sequence. Not-due auxiliary pages retain their original observation; an
attempted failure replaces that page with unavailable data. Source ages and
expiry use monotonic time. Repeated Prometheus timestamps are not new gateway
acquisitions.

Each `xb3_gateway_snapshot` retains parsed values, raw decimal codeword totals,
page retrieval bounds, group errors, startup identity and collection sequence.
For a recorded no-reply window, `xb3_loss_correlation_window` links the nearest
usable counter-context retrieval completed before launch and a usable request
started after completion. Ordering uses monotonic time within the same startup.
Missing prior context, failed intervening requests, a 120-second wait without a
usable later observation and local queue limits are explicit. UTC clock changes
are flagged without turning them into network loss.

These are **HTTP retrieval brackets**, not backend acquisition bounds or causal
attribution. Counter increments may remain visible after a brief event, but do
not timestamp IP loss. A short RF/lock/link excursion can disappear between
gateway reads. Steady later RF values cannot clear the RF path during a burst.
The portal cannot uniquely separate modem electronics, coax/drop/tap, shared
plant or gateway forwarding. It exposes no CPU utilization, forwarding-drop
counters or connection-table occupancy.

## Speed tests

Pings show delay and loss on a nearly idle line. Only filling the line shows
how much it carries, so the app runs speed tests: every 30 minutes by default,
at 7 and 37 minutes past the hour, and on request. `speedtest_interval_minutes`
accepts 0, 15, 30 or 60; 0 runs tests only on request. One test runs at a time.

The test uses Cloudflare's speed-test servers (`speed.cloudflare.com`) and the
method of its published engine, `@cloudflare/speedtest` 1.15.0 (MIT). That
package cannot run in Node unchanged: it reads `window.location`, takes timings
from browser resource-timing entries and holds each download body in memory.
The app keeps its sequence and arithmetic and times requests with Node's own
clock instead:

- Idle latency: the median of 21 empty requests on a warm connection, minus the
  server time reported in `Server-Timing`. Jitter is the mean change between
  consecutive values.
- Bandwidth: sets of 100 kB, 1 MB, 10 MB and 25 MB requests in each direction.
  A direction stops growing once every request in a set takes over a second.
  Download time is ping plus body; upload time is request start to response
  headers. The result is the 90th percentile of request speeds.
- Loaded latency: empty requests on a second connection every 400 ms during each
  set, kept only from sets whose fastest request lasted at least 250 ms.

Cloudflare's own sequence continues to 100 and 250 MB downloads above about 200
Mbps. On 2026-10-31 the endpoint answered 403 to a 100 MB request, so the app
stops at 25 MB. Faster lines are measured with shorter requests.

Each test runs in a child process, so transfers and garbage collection cannot
delay the one-second probes, and is killed after 150 seconds. Download bodies
are discarded as they arrive and uploads repeat one 64 KiB buffer.

What a result covers: the gateway, cable line and Comcast's network as far as
Cloudflare's nearest site, recorded as `colo` (the `CF-RAY` suffix). Wi-Fi is
not in the path. To compare a device, use speed.cloudflare.com on it.
Speedtest.net uses other servers and another method, so its numbers are not
directly comparable. The app does not send results to Cloudflare's results
endpoint; Cloudflare still sees the requests.

Cost: a test fills the line for roughly half a minute. At 100 Mbps down and
20 Mbps up it moves about 170 MB down and 50 MB up. While it runs,
`xb3_speedtest_running` is 1 and this app's probes see the test's own load.
Delay or loss inside those windows is not evidence of a line fault.

Request a test through Supervisor stdin, which needs no new listener:

```json
{ "command": "speed_test", "id": "phone-button" }
```

From Home Assistant, call the Supervisor stdin service with app
`894a6dc5_xb3-telemetry` and that object as input. A request while a test runs
is rejected and logged as `xb3_speedtest_rejected`. A scheduled slot that finds
a test running is skipped and logged.

| Data               | Where                                                                                                                                                      |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Latest result      | `xb3_speedtest_download_bits_per_second`, `_upload_bits_per_second`, `_idle_latency_seconds`, `_idle_jitter_seconds`, `_loaded_latency_seconds{direction}` |
| Which test         | `xb3_speedtest_last_success_timestamp_seconds`, `xb3_speedtest_result_sequence`; the gauges hold the latest success until the next one                     |
| Test windows       | `xb3_speedtest_running`, `_last_started_timestamp_seconds`, `_last_finished_timestamp_seconds`                                                             |
| Outcomes           | `xb3_speedtest_runs_total{trigger,outcome}` with outcome `success`, `failed` or `timeout`; `xb3_speedtest_skipped_slots`                                   |
| Every test in logs | `xb3_speedtest_started`, `xb3_speedtest_result` (all values and every request's speed and duration), `xb3_speedtest_failed` with a reason                  |
| Summary for HA     | Read-only `GET /speedtest` on port 19090: running state and the latest result in Mbit/s and ms                                                             |

## Connect the existing storage

1. Install/update the app and set `gateway_password` in Options. The username
   defaults to `admin`, URL to `http://10.0.0.1`, and network schedule to 20 seconds.
   The password is not logged or exported.
2. Put [example-scrape.yaml](example-scrape.yaml) in the VictoriaMetrics app's own
   `/config`. This is separate from HA Core `/config`. The include contains a
   bare job list, without a `scrape_configs:` wrapper. Check the repository prefix
   in the target hostname. The example uses `894a6dc5-xb3-telemetry:19090`, a
   five-second interval and three-second timeout.
3. Restart the VictoriaMetrics app to load the include. This briefly pauses
   ingestion. Check `up{job="xb3-telemetry"}`, per-group quality, source age,
   worker state and advancing original observation timestamps. `up=1` establishes
   an HTTP response, not usable or newly acquired gateway data.
4. Verify this app's own VictoriaLogs stream using `xb3_exporter_started`, then
   actual `xb3_probe_outcome`, `xb3_gateway_snapshot` and gateway log records.
   The existing pipeline can retain JSON in `_msg`; `unpack_json` exposes its
   fields. Stdout alone does not establish storage delivery.

Port 19090 is internal to the app network; host mapping is disabled by default.
`/healthz` is process liveness. The normal probe rate produces about 259,200 raw
outcome rows per day before other records. Existing storage retention applies.

## Queries and event links

Keep `mode="live"` on ordinary probe queries. Local errors and skipped slots must
not be counted as packet loss:

```promql
sum by (target) (
  increase(xb3_probe_outcomes_total{job="xb3-telemetry",mode="live",outcome="no_reply_by_deadline"}[1m])
)
```

This count window is useful for locating an episode. Raw VictoriaLogs rows retain
the individual timing and sequence:

```text
_stream:{container_name="app_894a6dc5_xb3-telemetry"}
| unpack_json
| filter mode:="live" AND event:="xb3_probe_outcome" AND outcome:="no_reply_by_deadline"
```

Use `event:="xb3_loss_correlation_window"` for the bracket records. Their probe
sequences and before/after collection sequences identify the corresponding raw
events under the same `startup_id`. Retrieve `xb3_gateway_snapshot` events for
those sequences and inspect their data and quality. A complete bracket requires
`bracket_complete=true`; a missing bracket is not evidence of a healthy gateway.

In VictoriaMetrics, `xb3_exporter_start_timestamp_seconds` matches
`startup_seconds` in logs. `xb3_gateway_collection_sequence` identifies the last
completed collection; `xb3_page_observation_sequence` identifies each cached
page, including older auxiliary pages. IDs and changing timestamps are not
Prometheus labels.

| Data             | Metrics / interpretation                                                                                                                     |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Probe results    | `xb3_probe_outcomes_total{mode,target,outcome}`, launch/schedule counters and RTT histogram; counters preserve short changes between scrapes |
| Recorder quality | Per-target last completion, scheduler lag, maximum launch gap, in-flight count, live worker state and local output-drop counter              |
| Gateway quality  | `xb3_collection_success`, original last-success time, page age, page sequence, worker state, cooldown and skipped slots                      |
| RF               | Lock, frequency Hz, power dBmV, downstream SNR and modulation at displayed UI positions                                                      |
| Codewords        | `xb3_downstream_codewords_total{slot,kind}`; raw cumulative totals, not IP packet-loss percentages                                           |
| Hardware         | Positive uptime, four Ethernet states/rates, reported DRAM/flash values and nonempty model/software metadata                                 |

RF and codeword slots are UI positions, not actual DOCSIS channel IDs. Their
tables are fetched separately and physical-channel correspondence is unverified.
Decreases can be resets or rollover. Use VictoriaMetrics for rates and qualify
windows with decreases or RF slot changes. Corrected errors are recovered errors.

The portal sometimes returns RF alongside blank metadata and false-zero uptime
or capacity. Groups are validated independently. The Internet flag is only a UI
value: missing backend data can show Inactive, while upstream `bridge-static`
code forces Active. Neither value proves external reachability.

DRAM retains `reported_unit="MB",unit_verified="false"`; the observed total
1048576 makes a byte conversion unestablished for this firmware. Available maps
to upstream `MemoryStatus.Free`, not established Linux `MemAvailable`. Neither
large used memory nor the static processor clock establishes overload. HA's
exact Ethernet port remains unmapped; the portal supplies no port error counters.

## Retained gateway logs

Firewall snapshots default to every 900 seconds in a separate session, starting
after one minute. `off` disables them. `all` adds System/Event, whose observed
queries each took about 121 seconds and returned empty arrays. Slow-category
isolation is unverified. Timeout stops the entire poll because PHP work may
continue after cancellation.

Today and Yesterday cover gateway midnight. Each record retains query ID,
window, row index, collector UTC, raw source time and original decoded row.
Repeated queries can return the same records. Counts are not accumulated as new
events. Gateway timestamps have no verified timezone. Observed IPv6 Firewall
drops with placeholder endpoints do not explain the existing IPv4 probe loss.

## Finite pipeline verification

Authenticated Supervisor stdin accepts this command once per process startup:

```json
{ "command": "verify_correlation", "id": "operator-chosen-run" }
```

From the HA Terminal & SSH app, submit it to Supervisor as follows. The app token
passes through curl's stdin configuration without being printed or placed in
the command arguments:

```sh
printf 'header = "Authorization: Bearer %s"\n' "$SUPERVISOR_TOKEN" |
  curl --fail --silent --show-error --config - \
    --header 'Content-Type: application/json' \
    --data-binary '{"command":"verify_correlation","id":"operator-chosen-run"}' \
    http://supervisor/addons/894a6dc5_xb3-telemetry/stdin
```

There is no HTTP control endpoint, arbitrary destination or shell command. After
a six-second zero-counter baseline, a synthetic transport runs the production
recorder for 20 scheduled slots per target while live probes continue. Its known
60-row pattern has 52 replies, six no-reply results and two local errors. External
sequences 5–7 simulate a short loss run; sequence 9 simulates a local error.
Every test row has `mode="validation"`, `synthetic=true` and the validation ID.
It changes no live replies, firewall rules or gateway measurements. It does not
automatically replay after restart.

Verify the exact raw outcome order and every target/sequence in VictoriaLogs,
and reconcile the stored validation counters in VictoriaMetrics with those rows.
Check that correlation records reference real surrounding gateway snapshots,
with monotonic bounds in the correct order and explicit quality. Recheck live
probe continuity, actual launch spacing, source collection and resource use.
This proves the pipeline preserves a known short pattern and its retrieval
context. It does not prove that a real WAN loss or RF excursion happened.

## Development

Use Node 22.22.2 or newer. From `rootfs/opt/xb3-telemetry`, run `npm ci
--ignore-scripts`, `npm test` and `npm run build`. The Docker build also checks
types and tests. `XB3_PASSWORD` supplies the secret for local runs. `--once`
prints one portal collection and `--logs-once` retrieves the configured log
categories; neither starts continuous probes. Normal service mode does. After
`npm run build`, `node dist/speed-child.js` runs one speed test and prints its
JSON result; it needs no gateway password.

`XB3_NETWORK_INTERVAL_SECONDS` accepts 20 or 60.
`XB3_SPEEDTEST_INTERVAL_MINUTES` accepts 0, 15, 30 or 60. Existing URL, username, log mode,
log interval, listen host/port and options-file environment overrides remain.
Fixtures preserve sanitized complete and partial TG1682P interface structures.
This implementation targets that observed PHP interface, not every XB3 model.

Sources: [HA extra scrape files](https://github.com/VictoriaMetrics-Community/homeassistant-addon-victoriametrics/blob/main/victoria-metrics/prometheus.tpl),
[network rendering](https://github.com/rdkcentral/webui/blob/dd9ad14f97bec8dc94fc45045c9b86d3e0869b38/source/Styles/xb3/code/network_setup.php),
[hardware mappings](https://github.com/rdkcentral/webui/blob/dd9ad14f97bec8dc94fc45045c9b86d3e0869b38/source/Styles/xb3/code/hardware.php),
[log handler](https://github.com/rdkcentral/webui/blob/dd9ad14f97bec8dc94fc45045c9b86d3e0869b38/source/Styles/xb3/code/actionHandler/ajax_troubleshooting_logs.php).
Published upstream code does not establish the deployed firmware's exact binaries.
Speed method: [`@cloudflare/speedtest`](https://github.com/cloudflare/speedtest).
