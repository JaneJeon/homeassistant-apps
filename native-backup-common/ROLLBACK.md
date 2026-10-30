# Roll back the app wrapper without losing newer database writes

This procedure is for an operator moving a database from the native-backup app
back to a legacy app. It is not an engine downgrade. It requires a maintenance
window with both writers stopped. Do one engine at a time.

The pre-cutover backup is a historical recovery point. Once the native app has
accepted new writes, starting that old copy would discard those writes. Transfer
the latest **quiesced live database**, preserve it in a new encrypted bundle, and
verify the transfer before starting the legacy writer.

## 1. Identify the exact source, destination, and binaries

- Record the native app slug, legacy app slug, container names, and pinned image
  digests. Verify the binaries in those images: VictoriaMetrics **1.153.0** or
  VictoriaLogs **1.52.0**, as applicable. Test a target image's version in a
  disposable container without network or database mounts. Do not assume an app
  version identifies its engine version. An older engine needs a separately
  proven migration procedure; do not start it against these files.
- Read `docker inspect --format '{{json .Mounts}}' <container>` for each writer.
  Resolve the native container's `/data` mount to its host source, then identify
  its `live` directory. Resolve the legacy container's `/share` mount and verify
  its configured storage path. The usual in-container legacy paths are
  `/share/victoria-metrics-data` and `/share/victoria-logs-data`; verify them rather
  than assuming their host equivalents. Do not print credential-bearing options
  or process arguments into the evidence log.
- Record the resolved paths. Refuse symlinks, overlapping source/destination
  trees, or a destination that is not the verified legacy storage directory.
  Stage under the destination's parent so promotion stays on one filesystem.
- Save fixed-time historical query results before stopping the source. Use the
  same queries and absolute time ranges after transfer: recent, seven-day-old,
  and older metrics where available; recent and older log partitions. Health
  checks alone do not demonstrate that history survived.

## 2. Stop writes and preserve the latest source

Pause the backup schedule and any automation or watchdog that can start either
app. Keep the legacy app stopped. Stop the native app gracefully through
Supervisor, wait for shutdown, and verify both containers remain stopped. Do not
begin a cold copy from a running database. If shutdown does not complete, stop
this procedure and investigate.

Run the published native runtime's `validate_native(source)` and
`inventory(source)` against the stopped live tree, mounted read-only in a
disposable container with no network. `inventory` records each relative file
path, byte length, and SHA256, and rejects symlinks and non-regular files. Save
that inventory privately, together with the engine version, image digests,
installation identity, verified mount paths, shutdown time, and fixed queries.
An unexpected file type or invalid native layout requires investigation; do not
silently omit files to make validation pass.

Create a **new encrypted bundle of this quiesced source**, its inventory, and the
configuration/identity needed to recover it. Use SecureTar v3 or an equivalently
authenticated encrypted archive. Supply the key from protected storage or a
0600 file, never command arguments or logs. Reopen the bundle with that key and
verify every archived source file against the saved inventory; record the
encrypted bundle's SHA256. Keep a verified copy outside the destination
filesystem before changing its existing contents.

An ordinary HA backup taken after the app stops does not substitute for this
bundle: Supervisor skips stopped-app hooks, `/data/live` is excluded, and the
last published export can predate the final writes. Keep the original stopped
source intact throughout this procedure.

## 3. Journal and stage the transfer

If the native app already has `operation.json`, resolve that interrupted
operation first. Do not overwrite or delete an unexplained journal.

Create a durable `operation.json` in the verified native `/data` host directory.
Record `operation: rollback`, a unique operation ID, source identity and inventory
digest, encrypted bundle digest, both image digests, resolved source/destination
paths, unique staging and quarantine paths, and phase `copying`. This journal
also makes accidental native startup fail closed. The legacy app does not honor
this journal; keeping its automatic startup disabled is essential.

Use the runtime's durability pattern for each journal update: write a temporary
file, flush and `fsync` it, replace the journal, then `fsync` its parent directory.

Create a new private staging directory beside the legacy destination. Refuse an
existing staging or quarantine path. Confirm staging and destination parent
have the same filesystem device ID. Copy the complete stopped live tree into
staging as independent files; do not hardlink it to the preserved source. Ensure
space for staging and the retained existing destination. A space check is only
an estimate: any copy, checksum, or sync error stops the procedure.

Recompute the source and staging inventories. Both must exactly equal the saved
source inventory. Run `validate_native(staging)`. `fsync` every staged regular
file and directory and the staging parent. Only then durably set phase
`prepared`.

VictoriaMetrics `vmbackup` output is not a live database directory. This procedure
copies stopped `/data/live`. If recovering from an export instead, first use
version-matched `vmrestore` into staging, then validate the resulting native
layout. VictoriaLogs export payloads contain `partitions/YYYYMMDD`; preserve that
layout when materializing them. Neither route permits copying partial results
directly into the legacy destination.

## 4. Promote while both writers remain stopped

Recheck that neither writer has restarted. If the legacy destination exists,
rename it to the recorded, unique quarantine sibling and `fsync` the parent;
durably record phase `old_quarantined`. Never delete or overwrite that copy.
Rename the verified staging directory to the legacy destination, `fsync` the
parent, and durably record phase `promoted`. Each rename is atomic on this
filesystem; the two renames are not one transaction. The journal and stopped
writers make an interruption between them recoverable.

If interrupted, keep both writers stopped. Inspect the recorded paths and
inventories to establish which rename completed. Complete or reverse that
specific operation without discarding any source. Do not start an app merely
because its expected directory exists, and do not let a missing directory cause
the legacy engine to initialize an empty database.

## 5. Verify the legacy writer before accepting the rollback

Verify the promoted inventory once more and confirm the legacy app will open
that exact path with the verified, same-version engine. Start only the legacy
app. Check its health and repeat the saved fixed-time historical queries;
results must match. Record the active image digest, query comparisons, and
outcome. On any failure, stop the legacy app and leave both writers stopped.
Preserve its failed-attempt tree separately before retrying: startup may already
have modified files. If it accepted writes, that tree is now the latest source
for the next transfer; do not overwrite it with the older native copy.

After success, retain the native source, new encrypted bundle, old destination
quarantine, and evidence. Mark the operation complete but retain the native
`operation.json` so the retired native writer cannot accidentally reopen its
now-aging copy. Re-enable automatic startup only for the verified active writer.

The native backup integration expects the native apps and their exports. A
successful wrapper rollback does not make legacy backups valid under that
contract. Keep backup health unverified/failed and retain pending-shred originals
until the backup workflow for the active storage has been restored and a new
complete cloud backup has passed validation. Do not resume a schedule that would
copy a running legacy database from Share.

## Returning to the native app after legacy writes

Repeat the same procedure in the opposite direction: stop both writers, preserve
the **latest legacy live tree** in a new verified encrypted bundle, and copy it
into fresh staging on the native `/data` filesystem. Verify identical inventories,
native layout, and same-version compatibility; quarantine the old native `live`
tree and atomically promote the new staging tree with a durable operation journal.
Retain the completed rollback record and durably supersede its journal with the
new reverse-transfer operation; do not simply clear the startup guard.

Use the verified installation identity for `live-ready.json`, write it durably
only after promotion, and remove the journal with a directory `fsync` only when
that promotion is committed. Start only the native writer and compare the fixed
queries before restoring normal operation. Its previous export is now stale;
create and validate a fresh complete backup.

Changing `bootstrap_mode` to `import_legacy` on an already initialized native app
does not perform this transfer. Deleting its journal or starting its old `live`
tree would reopen the pre-rollback copy and lose the intervening legacy writes.
