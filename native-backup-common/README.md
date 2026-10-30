# Native snapshot backup lifecycle

The VictoriaMetrics and VictoriaLogs apps use this shared runtime to keep serving while Home Assistant backs them up. The live database is private to its app. Share remains available for Paperless and other applications.

## What an app backup contains

`/data/live` holds the running database and `/data/work` holds temporary files. Both directories are excluded from the app archive. `/data/backup` contains a complete, stable native export and its manifest. `/data/installation.json` identifies the installation. The manifest records the engine and format versions, snapshot interval, partition set, generation and file checksums.

The hot backup hook creates a fresh export before Supervisor copies the app. VictoriaMetrics uses its snapshot API and version-matched `vmbackup`. VictoriaLogs snapshots its active partitions and materializes their contents. The hook publishes an export only after checking the files. It does not restart the database or change the committed export after returning.

Supervisor skips hooks for stopped apps. An older export can therefore appear in a later archive. The `native_snapshot_backups` integration in `homeassistant-config` checks the actual B2 artifact and rejects stale or missing snapshots. Home Assistant's completed event alone is insufficient evidence of a complete backup.

## Startup and restoration

The default `bootstrap_mode` is `restore_only`. An initialized live database opens normally. If live data is absent, startup requires a matching installation marker and valid native export. It restores into temporary storage, checks the result and atomically promotes it. Missing, corrupt or interrupted state stops startup instead of creating an empty database.

For an explicit legacy migration, stop the old app and select `import_legacy`. The app already mounts Share read-only. The runtime verifies and copies that engine's fixed legacy directory before promotion. Return the option to `restore_only` after import. Never run the old and new writers against the same source. An operation journal requires inspection after an interrupted promotion; do not remove it merely to make startup continue.

After the replacement has accepted writes, a rollback must transfer that latest data. Follow the [rollback procedure](ROLLBACK.md) to preserve a fresh encrypted copy, verify staging and promote it while both writers are stopped.

`native-backup startup init-empty` is an explicit initialization command for disposable test databases. It is not exposed as a production app option.

## Building and checking changes

These two Dockerfiles use the repository root as their build context so the runtime has one source:

```sh
docker build --platform linux/amd64 -f victoria-metrics/Dockerfile -t native-vm:test .
docker build --platform linux/amd64 -f victoria-logs/Dockerfile -t native-vl:test .
python3 -m unittest discover -s native-backup-common/tests -v
python3 native-backup-common/tests/integration.py
```

Repeat the builds and integration script with `linux/arm64`, tags `native-vm:arm64-test` and `native-vl:arm64-test`, and environment variables `TEST_PLATFORM=linux/arm64 TEST_IMAGE_TAG=arm64-test`.

The integration check ingests records, exports with an unchanged database process, imports a stopped legacy database, removes live data and queries the restored records. Release acceptance additionally requires downloading an encrypted HA backup from B2 and restoring it through HA/Supervisor into an isolated HA OS VM. Record the artifact ID and query comparisons, and repeat that drill after changes to the backup format or bootstrap.
