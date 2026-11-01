# Paperless Intake

Checks each scan before it goes into Paperless-ngx, and only commits scans that pass.

For each scan in `/share/paperless/capture`, the app:

1. Picks it up once it has stopped changing for 30 seconds and opens as a complete PDF.
2. Sends it to Google Document AI for OCR, using the pinned processor version.
3. Builds a searchable copy. The original page images stay byte-for-byte, Google's text sits invisibly on top, pages follow the printed page numbers, and pages are turned upright.
4. Checks the result:
   - Google's answer covers every page.
   - The text lines up with the page.
   - The images are unchanged.
   - The printed page numbers don't reveal a missing page.
5. Commits it to Paperless through the REST API, with the original as version 1 and the searchable copy as version 2. Then it reads both back and checks the bytes, the tags and the search text.

A scan that fails a check is **held**. Home Assistant gets an alert that names the problem, and the scan waits on the **Mail intake** page in the sidebar. From there it can be kept as the original only, or discarded to be rescanned. Alerts never include document text.

The app never deletes or edits anything in Paperless except the document it just created. It uploads that document with the Physical Mail and Mail intake tags, then only adds a version and a note.

Scans are worked on one at a time, each in a short-lived worker process. On a synthetic 15-page scan at 300 dpi, the worker peaked at about 350 MiB while building and checking the searchable copy. The worker then exits, so the app idles at about 90 MiB.

## Paperless setup

- **A dedicated user** for the app's API token. It needs to add and change documents, add notes, and view tags.
- **Two tags:** `Physical Mail`, and `Mail intake`, which marks what this app uploaded. If either is missing, nothing commits, and scans wait with an alert.
- **The "document added" ingest workflow must skip the `Mail intake` tag.** Add the filter "Does not have these tags: Mail intake" to its trigger. The app already signals Home Assistant itself, so without the filter each scan counts as finished twice. The workflow's source filter can't do this: Paperless checks sources only on "consumption started" triggers. The end-to-end tests check both points.

## Options

Put both secrets in Home Assistant's `secrets.yaml` and reference them here, so they live with your other secrets:

```yaml
paperless_token: "!secret paperless_intake_token"
google_service_account: "!secret paperless_intake_google_key"
```

In `secrets.yaml` the Google key is the service account's JSON key, as a multi-line value:

```yaml
paperless_intake_google_key: |
  {
    "type": "service_account",
    ...
  }
```

- **Paperless URL**: the Paperless app's internal address.
- **Paperless API token**: the token of the dedicated Paperless user (see Paperless setup).
- **Physical Mail tag** and **Intake tag**: the two tag names (see Paperless setup).
- **Google service-account key**: the Document AI service account's key. The app loads it from options; no key file is written.
- **Webhook IDs**:
  - `webhook_started` and `webhook_finished` are the existing ingest-watchdog webhooks.
  - `webhook_alert` feeds the alert automation.
- **Limits**:
  - `daily_page_cap` stops runaway Google use.
  - `min_free_mib` pauses work when the disk is low.
  - `keep_local_copies_days` sets how long local PDFs stay after a verified commit.

## Paperless versions

`rootfs/app/tested-paperless-version` names the Paperless version the end-to-end tests passed on. Keep auto-update off for the Paperless app.

The app commits only when Paperless reports one of two versions:

- the tested version, or
- a version the app has already committed under while that version was the tested one. These are recorded in `/share/paperless/intake/accepted-paperless-versions.json`.

The second rule matters between merging a version bump and updating Paperless: the app keeps committing on the older, previously tested version. Any other version, such as one Paperless reached by an update nobody tested, stops commits. Scans then wait with an alert naming both versions.

When Paperless releases, Renovate opens a pull request bumping that file, and CI reruns the tests against the new version. Merge when green, rebuild this app, then update Paperless.

## Files

All on `/share`, so the nightly backup covers them:

- `/share/paperless/capture/`: where the scanner saves.
- `/share/paperless/intake/items/<sha256>/`: one folder per scan, holding the original, the searchable copy, `checks.json`, `events.jsonl` and `state.json`. The PDF copies are removed `keep_local_copies_days` after a verified commit; the rest of the record stays.
- `/share/paperless/intake/duplicates/` and `/share/paperless/intake/discarded/`: kept for 30 days.
