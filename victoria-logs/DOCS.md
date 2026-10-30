# Configuration

Copy the old VictoriaLogs app's options during migration.

- `bootstrap_mode`: `restore_only` by default. Use `import_legacy` only for a confirmed stopped legacy database at `/share/victoria-logs-data`, then return to `restore_only`.
- `retention`: defaults to `2w`. Preserve the installed retention during migration.
- `enableHTTPAuth`, `username`, `password`: preserve the existing authentication settings.
- `additionalArguments`: additional command arguments. Managed storage, listener, authentication and snapshot-management flags cannot be overridden.
- `homeassistantUrl`: retained for compatibility with existing app options.

Port 9428 and the ingress log UI remain available. Native snapshots cover the returned partition set over a recorded interval; they are not a single atomic snapshot across every partition.

Backups and restore behavior are documented in [the shared lifecycle](../native-backup-common/README.md). Keep encryption keys and credentials outside the repository.
