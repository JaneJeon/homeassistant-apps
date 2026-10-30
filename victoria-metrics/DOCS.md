# Configuration

Copy the old VictoriaMetrics app's options and additional app configuration files during migration. The generated scrape configuration still includes `/config/*.yaml` and `/config/*.yml`, including the XB3 scrape file.

- `bootstrap_mode`: `restore_only` by default. Use `import_legacy` only for a confirmed stopped legacy database at `/share/victoria-metrics-data`, then return to `restore_only`.
- `retention`: defaults to `1y`. Preserve the installed retention during migration.
- `enableHTTPAuth`, `username`, `password`: preserve the existing authentication settings.
- `enablePrometheusScrape`, `prometheusScrapeHTTPS`, `prometheusScrapeInterval`, `prometheusScrapeTimeout`, `longelivedToken`, `homeassistantUrl`: preserve the existing Home Assistant scrape settings and credentials.
- `additionalArguments`: additional command arguments. Managed storage, listener, authentication, snapshot and scrape-configuration flags cannot be overridden.

Port 8428 and the ingress VM UI remain available. Graphite port 2003 is disabled by default, as in the previous app.

Backups and restore behavior are documented in [the shared lifecycle](../native-backup-common/README.md). Keep encryption keys and credentials outside the repository.
