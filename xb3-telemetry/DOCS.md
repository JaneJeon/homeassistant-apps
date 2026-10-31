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

### How a test measures

A test keeps the line full and reads how fast data moves, every quarter
second. It uses Cloudflare's speed-test endpoints (`speed.cloudflare.com`,
`/__down` and `/__up`), but not its published engine or its arithmetic: the app
computes every number itself from its own readings.

- Each direction runs for 10 seconds over 2 parallel connections. Download
  first, then upload, half a second apart.
- Downloads ask for 25 MB at a time and are cut off at 10 seconds. Bytes are
  counted when they arrive.
- Uploads send chunks and count a chunk only once Cloudflare confirms it
  arrived. Its bytes are spread evenly over its transfer. Each chunk is sized
  from the previous one's speed to take about half a second (64 KiB to 4 MiB),
  so an upload slowdown shows within about half a second, as a download one
  does. A fixed large chunk would take seconds on a slow line and average a
  short dip away.
- The first 2 seconds are dropped while the connections speed up. The other
  8 seconds give 32 readings per direction.
- From the readings: sustained is the median, worst the 10th percentile, best
  the 90th. Every reading is logged, so any other figure can be computed later.

At about 117 Mbit/s down and 24 up, a test makes about 6 download and 40 upload
requests and moves about 150 MB down and 30 MB up. It takes about 25 seconds.

### What a result covers, and what it cannot

A result covers the gateway, the cable line and Comcast's network as far as
Cloudflare's nearest site, recorded as `colo` (the `CF-RAY` suffix). Wi-Fi is
not in the path. Traffic from other devices shares the line during a test and
lowers its readings. The app cannot tell that traffic apart.

While a test runs, `xb3_speedtest_running` is 1 and this app's probes see the
test's own load. Delay or loss inside those windows is not evidence of a line
fault. Delay under load comes from those probes, not from the test.

To compare a device, use speed.cloudflare.com on it. That page uses Cloudflare's
own method, so its figures are close but not the same. Speedtest.net uses other
servers and another method.

### Limits: Cloudflare's, and the app's budget

Cloudflare refuses tests from an address that tests too much (HTTP 429) and
does not publish the limit. On 2026-10-31, four tests in 30 minutes from the
Green were followed by refusals for over an hour. A refusal seen that day, at
another address, asked for a 3600-second wait (`Retry-After`). So the app:

- Starts at most 3 tests in any rolling hour, scheduled and requested together.
  A request over the budget is refused with the time the next one may run.
- After a refusal, waits as long as `Retry-After` asks. Without one it waits
  60 minutes, doubling for each refusal in a row up to 4 hours.
- Records every scheduled slot: tested, rate limited, over budget or failed.
  A slot without a result is a marked gap, never a number.

The budget and the wait are kept in memory. After the app restarts, it does not
know about a wait Cloudflare asked for earlier.

### Requests and data

Request a test through Supervisor stdin, which needs no new listener:

```json
{ "command": "speed_test", "id": "phone-button" }
```

From Home Assistant, call the Supervisor stdin service with app
`894a6dc5_xb3-telemetry` and that object as input. A request while a test runs,
while Cloudflare's wait lasts or over the budget is refused and logged as
`xb3_speedtest_rejected` with the reason.

| Data               | Where                                                                                                                                                             |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Latest result      | `xb3_speedtest_bits_per_second{direction,stat}`: direction `download` or `upload`, stat `sustained`, `worst` or `best`; `xb3_speedtest_readings{direction}`       |
| Which test         | `xb3_speedtest_last_success_timestamp_seconds`, `xb3_speedtest_result_sequence`; the gauges hold the latest success until the next one                            |
| Every slot         | `xb3_speedtest_last_attempt_outcome` (1 tested, 2 rate limited, 3 over budget, 4 failed) and `xb3_speedtest_last_attempt_timestamp_seconds`                       |
| Test windows       | `xb3_speedtest_running`, `_last_started_timestamp_seconds`, `_last_finished_timestamp_seconds`                                                                    |
| Limits             | `xb3_speedtest_budget_per_hour`, `_tests_last_hour`, `_rate_limited_until_timestamp_seconds` (0 when not waiting)                                                 |
| Counts             | `xb3_speedtest_runs_total{trigger,outcome}`: `success`, `rate_limited`, `failed`, `timeout`; `xb3_speedtest_skipped_slots_total{reason}`                          |
| Every test in logs | `xb3_speedtest_started`; `xb3_speedtest_result` with the three figures, bytes, requests and every reading; `xb3_speedtest_failed` with a reason and any wait      |
| Summary for HA     | Read-only `GET /speedtest` on port 19090: status (`ready`, `running`, `rate_limited`, `over_budget`), when the next test may run, and the latest result in Mbit/s |

Each test runs in a child process, so transfers and garbage collection cannot
delay the one-second probes, and is killed after 90 seconds. Download bodies
are discarded as they arrive and uploads repeat one 64 KiB buffer.

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
