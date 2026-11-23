# App Disk I/O

This app records storage reads and writes for each running Home Assistant app, Core, Supervisor and their supporting containers. Use the history to see which workloads were reading or writing when the Green slowed down.

## Read the history

The collector sends cumulative byte and operation counters to VictoriaMetrics once a minute. To graph read throughput in MiB/s in Grafana Explore, use:

```promql
sum by (container_name) (rate(ha_container_io_read_bytes_total[5m])) / 1048576
```

For writes, use `ha_container_io_write_bytes_total`. To count bytes read across a longer incident, replace `rate` with `increase` and select the incident window. The operation counters are `ha_container_io_read_operations_total` and `ha_container_io_write_operations_total`.

Linux charges these reads and writes to a container across **all its block devices**. This includes the external SSD and internal eMMC. Network traffic and reads served from the Linux file cache are outside these counters. The System dashboard’s disk throughput measures the data disk separately.

The `container_name` label matches VictoriaLogs, for example `app_core_samba`. Each counter also includes the full Docker `container_id` so replacement containers keep distinct histories. Calculate rates before summing by name.

## Check coverage

Compare `ha_container_io_discovered` with `ha_container_io_accounted` and `ha_container_io_named`. A scope can disappear during a sample. Names come from the host journal in the background, up to four new lookups per minute. While a name is unavailable, the counters are recorded with the container ID and an empty name. Short-lived containers can start and exit between samples. Host processes outside Docker also use the disk.

`ha_container_io_collection_success` is 1 for a completed collection and 0 when a collection failed or timed out. A partial collection retains the counters it read. A host freeze or a stopped collector can leave a gap. If the metrics destination temporarily fails, the running collector retains the last 60 samples in memory with their original timestamps. `ha_container_io_dropped_samples_total` counts samples displaced from that buffer.

## Configuration

`metrics_url` is the VictoriaMetrics Prometheus import URL. It defaults to the VictoriaMetrics app in this repository. The recorder opens no listening ports. It reads systemd’s resource accounting over the host D-Bus and looks up container names in the read-only system journal. The collector runs with a non-root identity.
