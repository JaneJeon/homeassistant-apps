# Paperless Intake

Takes each scan from the scanner's folder, makes an upright, ordered, searchable copy of it with Google Document AI, and puts both the untouched scan and the copy into Paperless-ngx.

## What happens to a scan

For each scan in `/share/paperless/capture`, the app:

1. Picks it up once it has stopped changing for 30 seconds and opens as a complete PDF.
2. Sends it to Google Document AI for OCR, using the pinned processor version.
3. Decides, for the stack as a whole, which way up it is and whether it went in flipped (next section).
4. Builds the searchable copy. The page images are the scanner's own, byte for byte, in whatever format the scanner chose for each page (a scanner that detects colour mixes JPEG pages with one-bit black and white ones). Pages are turned upright and put in reading order. Google's text sits invisibly on top, word by word.
5. Checks that the printed page numbers don't reveal a missing page.
6. Commits to Paperless through the REST API: the untouched scan as version 1, the searchable copy as version 2. Then it reads both back and checks the bytes, the tags and the search text.
7. Adds a note to the document saying what it decided. The evidence behind each decision is in the scan's `checks.json` (see Files).

When the app uploads a scan, it gives Paperless the day of the scan as the document's date. It takes the day from the scanner's file name (`20261122_144510_000353.pdf`), or uses the day it picked the file up. Paperless would otherwise guess a date from its own reading of the raw scan. Documents uploaded before version 1.2.0 keep the date Paperless guessed.

## Any way the stack goes in

A mail piece goes into the feeder as one stack. It can go in correctly, turned head to foot, flipped over, or both. The app works out which, once per stack.

- **Turned head to foot.** Every image is upside down. A page with plenty of text keeps Google's own reading of which way up it is. A page with little text (a nearly empty back side) follows the rest of the stack whatever Google read for it, because a few words aren't enough to tell. A page printed sideways, or a back printed upside down, has plenty of text and is turned by itself.
- **Flipped over.** The whole page sequence comes out reversed, backs included: three double-sided sheets come out 6 5 4 3 2 1. So one reversal fixes single-sided and double-sided stacks alike, and the app doesn't need to know which sheets were double-sided.
  - With a complete printed series (`Page 1 of 4` to `Page 4 of 4`), pages follow the printed numbers exactly.
  - With only some numbers readable, the ones that were read vote: if they run backwards, the whole stack is reversed. A step down onto page 1 counts only when a total is printed (`Page 1 of 5`), because numbering that starts again at 1 usually means a second document. A number that is only referred to (`see page 4`) isn't counted.
  - With no page numbers, or numbers that don't settle it, the scan's order is kept and the note says it is unverified. There is nothing to check it against. A flipped stack with no page numbers stays reversed.

A wrong answer here leaves a page upside down or the pages in scan order. Every page image is still there, and the untouched scan is version 1. Pages can be turned or reordered later in Paperless.

## What holds a scan, and what never does

A scan is **held** in three cases. Each bears on whether the paper can be let go:

- **A page looks missing.** A printed series (`Page 1 of 4`) has a number that no page in the scan carries. One case is let through: only page 1 is unaccounted for, and the scan has a page with no number on it. Many letters print no number on their first page. Any other gap holds, even when the page might be there with its number unread, because only looking at the scan can tell.
- **The file isn't a readable PDF.**
- **The app kept failing for a day.** Something stopped it working on the scan (Paperless down, Google's setup, a defect in the app) and a day of retries didn't clear it. The first failure of that kind already sent a "waiting" alert.

A held scan waits on the **Mail intake** page in the sidebar. From there it can be kept as it is (both versions go into Paperless, with the gap named in the note) or discarded to be rescanned.

Nothing about the searchable copy holds a scan: not rotation, not page order, not where the invisible text sits, not the script the text is written in. Those can't lose a page, so the app decides as well as the evidence allows and writes the decision into the document's note. Version 1.0 held on all of them, and held the first two real scans it was given. When this version starts, it puts scans held for those reasons through the checks again.

If Google can't read one particular scan, or the app can't build the searchable copy from Google's answer, the untouched scan still goes into Paperless and an alert says so. It then has whatever text Paperless's own OCR gives it, or none if that is switched off (see Paperless setup). If Google refuses for a reason that would hit every scan (permission, a retired processor version, a wrong setting), scans wait and an alert says why.

A scan that hasn't finished going into Paperless when the app is updated is prepared again by the new version. When the app itself couldn't build the copy for a scan that is already in, the next version of the app tries once more, for as long as the local scan is kept (`keep_local_copies_days`). If it works, the copy is added to the same Paperless document as version 2, a second note says so, and the earlier warning is closed. If it doesn't, the document stays exactly as it was and nothing new is said. The app doesn't touch the document's owner or tags at that point; they are yours to have changed.

The invisible text covers the characters almost all mail uses (Unicode's basic plane, which includes Korean, Chinese, Japanese, Thai, Arabic and Hebrew). Arabic and Hebrew words are stored so they read back in the right order. Rarer characters outside that range are left out of the text layer, and the note says so.

## What you are told

- Nothing, when a scan went in normally. The document's note in Paperless has the details.
- **Held:** a page looks missing, the file can't be read, or a day of retries failed. Keep that paper.
- **Waiting:** something needs attention before scans can go in (Google's setup, an untested Paperless version, a crashed worker, an unexpected error in the app). Sent once per cause.
- **Warning:** the scan is in Paperless but something about it needs a look, such as no Google copy.
- **Resolved:** an earlier alert about a scan no longer applies.

Alerts never include document text.

The app never deletes anything in Paperless, and edits only documents it created itself. It uploads a document with the Physical Mail and Mail intake tags and clears its owner. After that it only adds a version and a note, either straight away or when a later version of the app builds a copy the first one couldn't.

Scans are worked on one at a time, each in a short-lived worker process. On a synthetic 15-page scan at 300 dpi, the 1.0 worker peaked at about 350 MiB. The worker then exits, so the app idles at about 90 MiB.

## Paperless setup

- **A dedicated user** for the app's API token, not a superuser. Give it these permissions and no others:

  | Type          | View | Add | Change |
  | ------------- | ---- | --- | ------ |
  | Document      | yes  | yes | yes    |
  | Note          | yes  | yes |        |
  | Tag           | yes  |     |        |
  | PaperlessTask | yes  |     |        |

  The end-to-end tests run the app as a user with exactly these permissions.

- **Two tags:** `Physical Mail`, and `Mail intake`, which marks what this app uploaded. The app's user must be able to see both: on each tag, either leave the owner empty or give that user View permission. If either tag is missing or hidden, nothing commits, and scans wait with an alert.
- **Owner.** Paperless makes an uploaded document its uploader's own, which hides it from other users who aren't superusers. The app clears that owner straight after upload, so its documents have no owner, like scans from the consume folder.
- **Paperless's own OCR and archive files.** Paperless has its own OCR (Tesseract), page rotation and deskew. Two settings under Configuration, OCR Settings, decide what they touch:

  | Mode | Archive file generation | What Paperless does |
  | --- | --- | --- |
  | `auto` | `auto` | The defaults. Paperless reads the raw scan itself and shows its own turned and cleaned rendition as version 1. It leaves the searchable copy alone, because the copy already has text. |
  | `off` | `never` | Paperless reads nothing and re-saves nothing. Version 1 is shown exactly as the scanner saved it, and a scan lands in seconds, not minutes. A scan that goes in without its Google copy has no search text until the copy is built. So does anything added to Paperless some other way. |
  | `redo`, `force` | or `always` | Paperless re-saves the searchable copy and shows its own file in its place. Don't use these. |

  The end-to-end tests run against the first two rows, and against `always` to prove the third is noticed. The app can't read these settings. After every scan it checks what Paperless did: if the copy got an archive file, the scan's alert says so.
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
- `/share/paperless/intake/items/<sha256>/`: one folder per scan, holding the original, the searchable copy, `checks.json`, `events.jsonl` and `state.json`. `checks.json` records what was measured for each page: the turn applied and why, the order decision and its votes, and how the text layer was placed. The PDF copies are removed `keep_local_copies_days` after a verified commit; the rest of the record stays.
- `/share/paperless/intake/duplicates/` and `/share/paperless/intake/discarded/`: kept for 30 days.
