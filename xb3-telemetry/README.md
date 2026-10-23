# XB3 Telemetry

TypeScript telemetry for the ARRIS TG1682P XB3: individually timed IPv4 probes,
passive gateway observations and explicit retrieval brackets for loss windows.
VictoriaMetrics scrapes `/metrics`; raw outcomes and snapshots use the existing
Vector → VictoriaLogs pipeline through app stdout.

See [DOCS.md](DOCS.md) for setup, metric meanings, and diagnostic limits.
