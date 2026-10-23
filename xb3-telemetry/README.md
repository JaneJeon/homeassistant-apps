# XB3 Telemetry

Read-only TypeScript exporter for the PHP admin portal on the ARRIS TG1682P XB3.
VictoriaMetrics scrapes `/metrics`; gateway log snapshots are written to app
stdout for the existing Vector → VictoriaLogs pipeline.

See [DOCS.md](DOCS.md) for setup, metric meanings, and diagnostic limits.
