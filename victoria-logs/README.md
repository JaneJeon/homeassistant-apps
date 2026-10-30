# VictoriaLogs with native snapshot backups

Runs VictoriaLogs 1.52.0 on port 9428. Data lives in the app's private storage and HA backups contain materialized native partition snapshots. The database continues running during routine backups.

Read [the shared lifecycle](../native-backup-common/README.md) before migration, backup or restore changes. A production installation requires explicit legacy import or a valid restored backup. Default startup cannot silently create an empty database.
