# Paperless-ngx Scanner

Takes each new scan from the scanner's folder, makes an upright, ordered, searchable version of it with Google Document AI, and puts both the untouched scan and the searchable version into Paperless-ngx. It does the same for scans that were stored before the app existed: each gets the searchable version, added as a newer file version. Every other document in Paperless is left alone.

The app used to be called Paperless Intake, and its tag used to be called Physical Mail.

A few words used below. A **new scan** is a file from the scanner's folder that isn't in Paperless yet. A **document** is something Paperless holds, and a **stored scan** is a document that came out of the scanner. Paperless keeps a history of file versions for each document: version 1 is the first file, and the newest version is the one it shows and searches. The **searchable version** is the file this app makes: the scanner's page images, upright and in order, with Google's text laid invisibly on top.

## What happens to a new scan

For each new scan in `/share/paperless/capture`, the app:

1. Picks it up once it has stopped changing for 30 seconds and opens as a complete PDF.
2. Sends it to Google Document AI for OCR, using the pinned processor version.
3. Decides, for the stack as a whole, which way up it is and whether it went in flipped (next section).
4. Builds the searchable version. The page images are the scanner's own, byte for byte, in whatever format the scanner chose for each page (a scanner that detects colour mixes JPEG pages with one-bit black and white ones). Pages are turned upright and put in reading order. Google's text sits invisibly on top, word by word.
5. Checks that the printed page numbers don't reveal a missing page.
6. Uploads to Paperless through the REST API: the untouched scan as version 1, the searchable version as version 2 (a later release may add newer ones). Then it reads both back and checks the bytes, the tags and the search text.
7. Adds a note to the document saying what it decided. The evidence behind each decision is in the scan's `checks.json` (see Files).

When the app uploads a new scan, it gives Paperless the day of the scan as the document's date. It takes the day from the scanner's file name (`20261122_144510_000353.pdf`), or uses the day it picked the file up. Paperless would otherwise guess a date from its own reading of the raw scan. Documents uploaded before version 1.2.0 keep the date Paperless guessed.

## Any way the stack goes in

A mail piece goes into the feeder as one stack. It can go in correctly, turned head to foot, flipped over, or both. The app works out which, once per stack.

- **Turned head to foot.** Every image is upside down. A page with plenty of text keeps Google's own reading of which way up it is. A page with little text (a nearly empty back side) follows the rest of the stack whatever Google read for it, because a few words aren't enough to tell. A page printed sideways, or a back printed upside down, has plenty of text and is turned by itself.
- **Flipped over.** The whole page sequence comes out reversed, backs included: three double-sided sheets come out 6 5 4 3 2 1. So one reversal fixes single-sided and double-sided stacks alike, and the app doesn't need to know which sheets were double-sided.
  - With a complete printed series (`Page 1 of 4` to `Page 4 of 4`), pages follow the printed numbers exactly.
  - With only some numbers readable, the ones that were read vote: if they run backwards, the whole stack is reversed. A step down onto page 1 counts only when a total is printed (`Page 1 of 5`), because numbering that starts again at 1 usually means a second document. A number that is only referred to (`see page 4`) isn't counted.
  - With no page numbers, or numbers that don't settle it, the scan's order is kept and the note says it is unverified. There is nothing to check it against. A flipped stack with no page numbers stays reversed.

A wrong answer here leaves a page upside down or the pages in scan order. Every page image is still there, and the untouched scan is version 1. Pages can be turned or reordered later in Paperless.

## What holds a new scan, and what never does

A new scan is **held** in three cases. Each bears on whether the paper can be let go:

- **A page looks missing.** A printed series (`Page 1 of 4`) has a number that no page in the scan carries. One case is let through: only page 1 is unaccounted for, and the scan has a page with no number on it. Many letters print no number on their first page. Any other gap holds, even when the page might be there with its number unread, because only looking at the scan can tell.
- **The file isn't a readable PDF.**
- **The app kept failing, or kept waiting, for a day.** Something stopped it working on the scan (Paperless down, Google's setup, a defect in the app, a Green that stayed short of memory) and a day of retries didn't clear it. A failure that needs Jane already sent a "waiting" alert the first time.

A held scan waits on the **Scans** page in the sidebar. From there it can be kept as it is (both versions go into Paperless, with the gap named in the note) or discarded to be rescanned.

Nothing about the searchable version holds a new scan: not rotation, not page order, not where the invisible text sits, not the script the text is written in. Those can't lose a page, so the app decides as well as the evidence allows and writes the decision into the document's note. Version 1.0 held on all of them, and held the first two real scans it was given. When a newer version starts, it puts scans held for those reasons through the checks again.

If Google can't read one particular scan, or the app can't build the searchable version from Google's answer, the untouched scan still goes into Paperless and an alert says so. It then has whatever text Paperless's own OCR gives it, or none if that is switched off (see Paperless setup). If Google refuses for a reason that would hit every scan (permission, a retired processor version, a wrong setting), new scans wait and an alert says why.

A new scan that hasn't reached Paperless when the app is updated is prepared again by the new version. Once its original is in Paperless, it finishes going in as it is, and from then on its document is looked after like every other stored scan (next section).

The invisible text covers the characters almost all mail uses (Unicode's basic plane, which includes Korean, Chinese, Japanese, Thai, Arabic and Hebrew). Arabic and Hebrew words are stored so they read back in the right order. Rarer characters outside that range are left out of the text layer, and the note says so.

## Scans that are already in Paperless

The app also looks after stored scans: the ones from before the app existed, and any whose searchable version failed the first time.

**The `Scanner` tag is the boundary.** It says: this came out of the scanner. The app puts the tag on every scan it uploads, and a Paperless rule put it on the scans that came through Paperless's own consume folder before the app. The app takes on a stored document only if it carries the tag. Nothing else decides: not the file's name, not its owner, not what is inside it.

- **A scan that is missing the tag** is a gap on that one document. Jane adds the tag in Paperless, and the app takes the document on at its next look. Nothing new is started while any document waits for tomorrow's Google pages. The app never changes a tag on a document already stored. It has no second way of recognising a scan, and shouldn't be given one.
- **A document without the tag is left alone:** a file added on Paperless's web page, a photo from the phone, an email attachment. The app doesn't fetch it, read it or add to it. With Paperless's own OCR off, such a file has search text only if it has text inside it already. That is known and accepted for now, and the missing tag is how to find those documents in Paperless.
- **The tag is checked every time** the app is about to work on a stored document. If it has been taken off, the app does nothing to the document and lets it go. If the tag comes back, the app takes the document on again. A document that already has its searchable version stays as it is.

Every five minutes, when no new scan is waiting, the app asks Paperless for the documents that carry the tag. Paperless sends no others. Nothing has to tell the app that a document arrived, so nothing is missed while the app is stopped. For each one it hasn't met, it fetches version 1 and looks at the file:

- **No text inside the file** (what the scanner saves): it gets a searchable version made the same way as for a new scan. Google reads it, the pages are turned upright and put in order, and the result is added as the newest version with a note. Version 1 stays as it was.
- **Text already inside the file** (a scanner set to read pages itself): left exactly as it is. There is nothing to read off the page images.
- **Not a PDF:** not something the scanner saves, so the tag on it is probably a mistake. The app sets it aside for good and lists it on the Scans page with the reason. Giving a photo the tag does not get it read.

The app keeps one small record per document (see Files). Every document ends in one of four outcomes: **done**, **not needed** (for good), **gave up** (for good, with the reason), or **next release** (this version of the app couldn't, and the next one tries once). A document whose tag was taken off is **let go** instead. The Scans page has a section listing them.

The same loop is what improves a searchable version later. If Paperless couldn't read all the words of one, or the app failed to build one, the next version of the app tries once, working from version 1 as Paperless holds it. If it works, the better searchable version is added as a newer file version, a second note says so, and any earlier warning about that scan is closed. If it doesn't, the document stays exactly as it was.

**The same file by two routes.** A new scan takes for its own only a document the app uploaded (it holds the scan's file and carries the `Mail Intake` tag). If the same file is already in Paperless some other way and then comes through the scanner's folder, the new scan becomes a second document, and Paperless flags the two as duplicates. If Paperless is set to refuse duplicates (`PAPERLESS_CONSUMER_DELETE_DUPLICATES`, off by default), that scan waits with an alert that says why.

What the app may change on a stored document: it adds one file version and one note. It never touches the owner, tags, date or title, and it never deletes anything. It adds a version only when every other file version on the document is one the app added itself. If you have added one of your own (by rotating pages in Paperless's editor, say), the app leaves that document alone for good.

A stored scan is never held and never raises an alert, because nothing depends on an answer: the document is already safe. A page that looks missing goes into the document's note. If Google or Paperless isn't answering, the app tries again each hour for a day, then leaves the document for the next version of the app. An unexpected error in the app gets three tries, and so does a worker that dies.

**A scan the app can't see.** Paperless hides a document that has an owner from every other user who isn't a superuser. A scan from Paperless's consume folder has no owner, and the app clears the owner on the scans it uploads, so those are visible. A scan that does have an owner (one that was added by hand on Paperless's web page belongs to whoever added it) is hidden from the app even with the tag on it. Clear the owner on that document, or give the app's user View and Change permission on it. The end-to-end tests check both halves: an owned document is invisible, and it is picked up once it is shared.

**If a scan that carries the tag isn't picked up,** look in this order:

1. Which version of the app is installed, on the app's page in Home Assistant. The work on stored scans starts with version 1.3.1.
2. The Scans page. It lists every stored scan the app has looked at, and the reason when nothing was done. If the app is waiting (next section), it says why at the top.
3. The document's owner in Paperless. If it has one, the app can't see it (above).
4. New scans and the day's Google pages come first, so a stored scan can wait until tomorrow.

## When the app waits

The app works on one thing at a time, from a queue kept in its own records: new scans first, in the order they arrived, then stored scans. Before it starts anything, it asks whether anything should start now. Each "no" comes with a reason in plain words, shown at the top of the Scans page and in Home Assistant (the Status and Paused entities, below).

Nothing starts while:

- **An upload may still be on its way into Paperless.** When an upload times out, or the app is stopped with one in flight, Paperless may still be taking the file in minutes later. Until Paperless shows how that upload ended, the app sends nothing else, so no file is sent again while it may still be on its way, and a struggling Paperless isn't given more. Uploads get 10 minutes to answer. How it ends:
  - **It landed:** the file is in Paperless (found by its checksum).
  - **Its task ended:** the job goes on by its own rules.
  - **It never landed:** its task is still running past Paperless's own time limit (`paperless_worker_timeout`, 30 minutes unless Paperless is set otherwise) and 15 minutes more, counted from when it started, or still queued six hours after Paperless was given it, or, when the app never got a task id, Paperless has no unfinished upload from after this one. Paperless's worker can die while it takes a file in (a power cut, a restart), and Paperless then leaves the task unfinished for good. The job sends the file again.
  - **You forget it:** the Scans page has a "Forget this upload" button while one is in doubt, for when you know Paperless will never finish it. It counts as never landed, at once, and the log says you chose it.

  A file that never lands three times is set aside: a new scan is held with an alert ("Paperless never finished taking this file in"), and a stored scan is set aside as gave up.
- **The Green is short of memory:** less than `min_available_mib` available (MemAvailable, default 500 MiB).
- **The disk is low:** less than `min_free_mib` free (default 1024 MiB).
- **The Green is stalling:** in the last minute, work waited on disk I/O for `max_io_pressure` % of the time or more (default 60), or on memory for `max_memory_pressure` % or more (default 40). These are the kernel's pressure figures (`/proc/pressure/io` and `/proc/pressure/memory`). With swap full, a shortage of memory shows up as disk I/O, which the memory figure alone doesn't see. On a kernel without these files the app goes by available memory alone, and says so once in its log at start. The defaults are set high on purpose, until there are real figures from the Green: once an hour the log has one line with both readings and the available memory, to set the bars by.

A new scan held back by one of these waits two minutes and is asked about again. It is listed under Retrying on the Scans page, with the reason and its next try. It isn't a failed try. If it lasts a day, the scan is held with an alert, like any scan that waited a day.

Stored scans also wait:

- **while a new scan is waiting for its turn**, because new scans go first;
- **while one stored scan waits on Paperless or Google.** Its own next try, an hour later, is the check that the service is back, so one outage can't run through every stored scan;
- **for two minutes after each one**, because Paperless goes on working on a new file version after it has taken it;
- **until tomorrow**, once stored scans have used their share of the day's Google pages (below).

**Killed at any moment.** A power cut, an out-of-memory kill or a restart by the Supervisor can stop the app anywhere. When it starts again it carries on every job from the first step not yet done, looking first at what is already there: the file in Paperless by its checksum, the upload's task, the note, the record. It never sends a file again while Paperless may still be taking it in, never counts Google's pages twice, and never sends "started" twice. A job that was in hand when the app stopped isn't counted as a failed try. Only a worker that dies while the app is running counts against its job. A job that takes the whole app down ten times at the same step is set aside: a new scan is held with an alert, a stored scan is left for the next release.

## What you are told

- Nothing, when a new scan went in normally. The document's note in Paperless has the details.
- Nothing, ever, from the work on stored scans. The document's note and the Scans page say what was done.
- **Held:** a page looks missing, the file can't be read, or a day of retries failed. Keep that paper.
- **Waiting:** something needs attention before new scans can go in (Google's setup, an untested Paperless version, a tag missing in Paperless, a crashed worker, an unexpected error in the app). Sent once per cause.
- **Warning:** the scan is in Paperless but something about it needs a look, such as no searchable version.
- **Resolved:** an earlier alert about a scan no longer applies.

Alerts never include document text. An alert is written into the scan's record with the change it is about, and sent from there: if Home Assistant doesn't take it, or the app is killed first, it is sent again later. So an alert can arrive twice, but is never lost.

The watchdog's two webhooks are sent at most once each per new scan: "started" when the app picks the scan up, and "finished" when it has ended (gone in, held or failed), however many restarts that took. A "finished" more than 45 minutes after "started" isn't sent, because the Home Assistant watchdog has already reported the scan.

The app never deletes anything in Paperless. It uploads a new scan with the `Scanner` and `Mail Intake` tags and clears its owner. Apart from that, all it ever does to any document is add a file version and a note.

Each job runs in a short-lived worker process. On a synthetic 15-page scan at 300 dpi, the 1.0 worker peaked at about 350 MiB. The worker then exits, so the app idles at about 90 MiB.

## The app's numbers in Home Assistant

The app publishes what it is doing to Home Assistant over MQTT, as entities of one device, **Paperless-ngx Scanner**. They appear by themselves (MQTT discovery). The app gets the broker's address and login from the Supervisor, so with the Mosquitto app installed there is nothing to set. Without a broker, or with it down, the app works as before and tries again every few minutes. Publishing never holds up the work.

| Entity | What it says |
| --- | --- |
| `sensor.paperless_ngx_scanner_status` | Idle, Scanning, Re-reading or Waiting. Waiting means only that a new scan is due and is being held back. Attributes: the reason in words, what it waits for, and until when |
| `binary_sensor.paperless_ngx_scanner_paused` | On while work is held back for an outside reason: Paperless, Google, memory, disk or disk I/O. Not for the day's page cap or the pause between stored scans, which are planned. Attribute: the reason |
| `binary_sensor.paperless_ngx_scanner_paperless_reachable` | Whether Paperless answers, asked at most once a minute |
| `sensor.paperless_ngx_scanner_scans_held` | New scans held for Jane to keep or discard. Attribute: each one's file name and reason |
| `sensor.paperless_ngx_scanner_scans_retrying` | New scans waiting to try again |
| `sensor.paperless_ngx_scanner_scans_failed` | New scans whose upload couldn't be confirmed ("Commit failed: keep the paper") |
| `sensor.paperless_ngx_scanner_last_scan` | When the last new scan went into Paperless |
| `sensor.paperless_ngx_scanner_google_pages_today` | Pages sent to Google today. Attributes: the day's cap, and stored scans' share of it |
| `sensor.paperless_ngx_scanner_google_pages_this_month` | Pages sent to Google this month. Google's free allowance is per month. Counting starts with version 1.4.0 |
| `sensor.paperless_ngx_scanner_stored_to_reread` | Stored scans still waiting for their searchable version, from Paperless's list as last asked and the records. Unknown until the app has asked once |
| `sensor.paperless_ngx_scanner_stored_set_aside` | Stored scans this release has set aside: gave up (not a PDF, say), or left for the next release after its tries ran out. Attribute: each document's id in Paperless and the reason |

Every value is worked out again from the records each time it is published: on every change, and every five minutes. All are sent retained, so Home Assistant has them as soon as it starts. If the app dies, every entity shows unavailable (the availability topic has a last will). Counts are whole numbers of scans or pages. Nothing published holds document text.

**"Today" and "this month" are local.** The Supervisor gives every app container Home Assistant's time zone as `TZ`, so the day starts at local midnight, and the month on the 1st. That is the day of the `daily_page_cap` too. Before version 1.4.0 the day started at midnight UTC.

## Paperless setup

- **A dedicated user** for the app's API token, not a superuser. Give it these permissions and no others:

  | Type          | View | Add | Change |
  | ------------- | ---- | --- | ------ |
  | Document      | yes  | yes | yes    |
  | Note          | yes  | yes |        |
  | Tag           | yes  |     |        |
  | PaperlessTask | yes  |     |        |

  The end-to-end tests run the app as a user with exactly these permissions. PaperlessTask is how the app asks how an upload ended.

- **Two tags.** `Scanner` (under `Source`) marks what came out of the scanner, and is how the app knows a stored document is its own. `Mail Intake` marks what this app uploaded: the app uses it to find its own uploads, and Jane's ingest workflow and the Home Assistant watchdog rely on it (next items). The app's user must be able to see both: on each tag, either leave the owner empty or give that user View permission. The app finds a tag by its name, whatever its case. If either tag is missing or hidden, nothing is uploaded, new scans wait with an alert, and stored scans wait. The app never stops for it.
- **From Physical Mail to Scanner.** Until 1.4.0 the scanner's tag was `Physical Mail`. Rename that tag in Paperless to `Scanner` once 1.4.0 is installed. Until then the app waits, as for any missing tag, and carries on by itself once the tag is there.
- **Owner.** Paperless makes an uploaded document its uploader's own, which hides it from other users who aren't superusers. The app clears that owner straight after upload, so its documents have no owner, like scans from the consume folder.
- **Paperless's own OCR and archive files.** Paperless has its own OCR (Tesseract), page rotation and deskew. Two settings under Configuration, OCR Settings, decide what they touch:

  | Mode | Archive file generation | What Paperless does |
  | --- | --- | --- |
  | `auto` | `auto` | The defaults. Paperless reads the raw scan itself and shows its own turned and cleaned rendition as version 1. It leaves the searchable version alone, because that already has text. |
  | `off` | `never` | Paperless reads nothing and re-saves nothing. Version 1 is shown exactly as the scanner saved it, and a new scan lands in seconds, not minutes. A new scan that goes in without its searchable version has no search text until one is built. A file with no text inside it that is added to Paperless some other way has no search text at all, because this app only looks after documents that carry `Scanner`. |
  | `redo`, `force` | or `always` | Paperless re-saves the searchable version and shows its own file in its place. Don't use these. |

  **This system runs the second row, `off` and `never`.** That is Jane's decision: Google is the only reader, with no fallback. The app works with the first row too, but there a new scan whose Google reading fails keeps Tesseract's text, which is the fallback she ruled out.

  The end-to-end tests run against the first two rows, and against `always` to prove the third is noticed. The app can't read these settings. After every new scan it checks what Paperless did: if the searchable version got an archive file, the scan's alert says so.
- **The "document added" ingest workflow must skip the `Mail Intake` tag.** Add the filter "Does not have these tags: Mail Intake" to its trigger. The app already signals Home Assistant itself, so without the filter each new scan counts as finished twice. The workflow's source filter can't do this: Paperless checks sources only on "consumption started" triggers. The end-to-end tests check both points.

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
- **Scanner tag** (`scanner_tag`, default `Scanner`) and **Intake tag** (`intake_tag`, default `Mail Intake`): the two tag names (see Paperless setup). The old option `physical_mail_tag` is gone. The Supervisor drops it from the stored options, with a warning in its own log.
- **Google service-account key**: the Document AI service account's key. The app loads it from options; no key file is written.
- **Webhook IDs**, none of which has a default. An empty one means: don't send.
  - `webhook_started` and `webhook_finished` are the existing ingest-watchdog webhooks.
  - `webhook_alert` feeds the alert automation.
- **Paperless worker timeout** (`paperless_worker_timeout`, default 1800 seconds): the same as Paperless's own `PAPERLESS_WORKER_TIMEOUT`. Set it to match if Paperless is set otherwise. See When the app waits.
- **Limits**:
  - `daily_page_cap` stops runaway Google use. New scans may use all of it. Stored scans may use three quarters of it.
  - `min_free_mib`, `min_available_mib`, `max_io_pressure` and `max_memory_pressure`: no new work starts while the Green is past one of them (see When the app waits).
  - `keep_local_copies_days` sets how long local PDFs stay after a verified upload.

## Paperless versions

`rootfs/app/tested-paperless-version` names the Paperless version the end-to-end tests passed on. Keep auto-update off for the Paperless app.

The app uploads only when Paperless reports one of two versions:

- the tested version, or
- a version the app has already uploaded under while that version was the tested one. These are recorded in `/share/paperless/intake/accepted-paperless-versions.json`.

The second rule matters between merging a version bump and updating Paperless: the app keeps uploading on the older, previously tested version. Any other version, such as one Paperless reached by an update nobody tested, stops uploads. New scans then wait with an alert naming both versions, and stored scans wait.

When Paperless releases, Renovate opens a pull request bumping that file, and CI reruns the tests against the new version. Merge when green, rebuild this app, then update Paperless.

## Files

All on `/share`, so the nightly backup covers them:

- `/share/paperless/capture/`: where the scanner saves.
- `/share/paperless/intake/items/<sha256>/`: one folder per new scan, holding the original, the searchable version, `checks.json`, `events.jsonl`, `claim.json` and `state.json`. `checks.json` records what was measured for each page: the turn applied and why, the order decision and its votes, and how the text layer was placed. The PDFs are removed `keep_local_copies_days` after a verified upload; the rest of the record stays.
- `/share/paperless/intake/documents/<Paperless document id>/`: one folder per document, holding `record.json` (what kind of file it is, whether it has its searchable version, which version of the app last worked on it, and the reason when nothing was done), `events.jsonl` (every change of that record) and `checks.json` (what was measured the last time a searchable version was made for it). Working files are removed when the work ends.
- `/share/paperless/intake/google-usage.json`: Google pages today and this month, and which of Google's answers have been counted.
- `/share/paperless/intake/upload-in-doubt.json`: there only while an upload's outcome isn't known (When the app waits). `upload-strikes.json` beside it counts the uploads of each file that never landed.
- `/share/paperless/intake/paperless-listing.json`: the ids of the tagged documents Paperless last listed, for the count of stored scans still to re-read.
- `/share/paperless/intake/duplicates/` and `/share/paperless/intake/discarded/`: kept for 30 days.

Every record is written whole or not at all (a temporary file, synced, renamed over the old one), so a kill never leaves half a record.
