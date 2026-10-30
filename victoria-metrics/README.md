# VictoriaMetrics with native snapshot backups

Runs VictoriaMetrics 1.153.0 on port 8428. Data lives in the app's private storage and HA backups contain a native `vmbackup` export. The database continues running during routine backups.

Read [the shared lifecycle](../native-backup-common/README.md) before migration, backup or restore changes. A production installation requires explicit legacy import or a valid restored backup. Default startup cannot silently create an empty database.
