# Paperless Intake

Takes each scan from the scanner's folder, makes an upright, ordered, searchable copy of it with Google Document AI, and puts both the untouched scan and the copy into Paperless-ngx. It does the same for scans that were stored before the app existed: each gets the searchable copy, added as a newer version. Every other document in Paperless is left alone.

Two words used below. A **scan** is a file from the scanner's folder that isn't in Paperless yet. A **document** is something Paperless holds. Paperless keeps a history of file versions for each document: version 1 is the first file, and the newest version is the one it shows and searches.

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

A scan that hasn't reached Paperless when the app is updated is prepared again by the new version. Once a scan's original is in Paperless, it finishes going in as it is, and from then on its document is looked after like every other stored scan (next section).

The invisible text covers the characters almost all mail uses (Unicode's basic plane, which includes Korean, Chinese, Japanese, Thai, Arabic and Hebrew). Arabic and Hebrew words are stored so they read back in the right order. Rarer characters outside that range are left out of the text layer, and the note says so.

## Scans that are already in Paperless

The app also looks after scans that are already stored: the ones from before the app existed, and any whose searchable version failed the first time.

**The `Physical Mail` tag is the boundary.** The app puts the tag on every scan it stores, and a Paperless rule put it on every scan that came through Paperless's own consume folder before the app. The app takes on a stored document only if it carries the tag. Nothing else decides: not the file's name, not its owner, not what is inside it.

- **A scan that is missing the tag** is a gap on that one document. Add the tag in Paperless, and the app takes the document on at its next look. The app has no second way of recognising a scan, and shouldn't be given one.
- **A document without the tag is left alone:** a file added on Paperless's web page, a photo from the phone, an email attachment. The app doesn't fetch it, read it or add to it. With Paperless's own OCR off, such a file has search text only if it has text inside it already. That is known and accepted for now, and the missing tag is how to find those documents in Paperless.
- The tag is looked at when the app first meets a document. Taking the tag off a document the app already has a record for doesn't make the app forget it.

Every five minutes, when no scan is waiting, the app asks Paperless for its list of documents. Nothing has to tell the app that a document arrived, so nothing is missed while the app is stopped. For each document that carries the tag and that it hasn't met, it fetches version 1 and looks at the file:

- **No text inside the file** (what the scanner saves): it gets a searchable version made the same way as for a scan on its way in. Google reads it, the pages are turned upright and put in order, and the result is added as the newest version with a note. Version 1 stays as it was.
- **Text already inside the file** (a scanner set to read pages itself): left exactly as it is. There is nothing to read off the page images.
- **Not a PDF** (a photo that was given the tag): left for a later version of the app that can read images.

The app keeps one small record per document (see Files). Every document ends in one of four outcomes: **done**, **not needed** (for good), **gave up** (for good, with the reason), or **next release** (this version of the app couldn't, and the next one tries once). The Mail intake page has a section listing them.

The same loop is what improves a searchable version later. If Paperless couldn't read all the words of one, or the app failed to build one, the next version of the app tries once, working from version 1 as Paperless holds it. If it works, the better copy is added as a newer version, a second note says so, and any earlier warning about that scan is closed. If it doesn't, the document stays exactly as it was.

**The same file by two routes.** A scan on its way in takes for its own only a document the app stored (it holds the scan's file and carries the `Mail intake` tag). If the same file is already in Paperless some other way and then comes through the scanner's folder, the scan becomes a second document, and Paperless flags the two as duplicates. If Paperless is set to refuse duplicates (`PAPERLESS_CONSUMER_DELETE_DUPLICATES`, off by default), that scan waits with an alert that says why.

What the app may change on a stored document: it adds one file version and one note. It never touches the owner, tags, date or title, and it never deletes anything. It adds a version only when every other file version on the document is one the app added itself. If you have added one of your own (by rotating pages in Paperless's editor, say), the app leaves that document alone for good.

A stored scan is never held and never raises an alert, because nothing depends on an answer: the document is already safe. A page that looks missing goes into the document's note. If Google or Paperless isn't answering, the app tries again each hour for a day, then leaves the document for the next version of the app. An unexpected error gets three tries. So does work that is cut short because the whole app stopped.

Three limits apply:

- **Scans on their way in go first.** The app works on one thing at a time, and a stored scan gets a turn only when no scan is waiting.
- **Stored scans use at most three quarters of `daily_page_cap`** in a day, so a new scan always has room. A document that doesn't fit today waits for tomorrow and goes first then. Until then no new document is started, so files aren't fetched only to wait. Waiting for tomorrow isn't counted as a failed try.
- **Version 1.3.1 starts on only three scans** that were stored before the app, so the result can be looked at in Paperless first. The version after it removes that limit.

**A scan the app can't see.** Paperless hides a document that has an owner from every other user who isn't a superuser. A scan from Paperless's consume folder has no owner, and the app clears the owner on the scans it stores, so those are visible. A scan that does have an owner (one that was added by hand on Paperless's web page belongs to whoever added it) is hidden from the app even with the tag on it. Clear the owner on that document, or give the app's user View and Change permission on it. The end-to-end tests check both halves: an owned document is invisible, and it is picked up once it is shared.

## What you are told

- Nothing, when a scan went in normally. The document's note in Paperless has the details.
- Nothing, ever, about a scan that was already in Paperless. Its note and the Mail intake page say what was done.
- **Held:** a page looks missing, the file can't be read, or a day of retries failed. Keep that paper.
- **Waiting:** something needs attention before scans can go in (Google's setup, an untested Paperless version, a crashed worker, an unexpected error in the app). Sent once per cause.
- **Warning:** the scan is in Paperless but something about it needs a look, such as no Google copy.
- **Resolved:** an earlier alert about a scan no longer applies.

Alerts never include document text.

The app never deletes anything in Paperless. It uploads a scan with the Physical Mail and Mail intake tags and clears its owner. Apart from that, all it ever does to any document is add a file version and a note.

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

- **Two tags:** `Physical Mail`, which marks what came out of the scanner and is how the app knows a stored document is its own, and `Mail intake`, which marks what this app uploaded. The app's user must be able to see both: on each tag, either leave the owner empty or give that user View permission. If either tag is missing or hidden, nothing commits, and scans wait with an alert.
- **Owner.** Paperless makes an uploaded document its uploader's own, which hides it from other users who aren't superusers. The app clears that owner straight after upload, so its documents have no owner, like scans from the consume folder.
- **Paperless's own OCR and archive files.** Paperless has its own OCR (Tesseract), page rotation and deskew. Two settings under Configuration, OCR Settings, decide what they touch:

  | Mode | Archive file generation | What Paperless does |
  | --- | --- | --- |
  | `auto` | `auto` | The defaults. Paperless reads the raw scan itself and shows its own turned and cleaned rendition as version 1. It leaves the searchable copy alone, because the copy already has text. |
  | `off` | `never` | Paperless reads nothing and re-saves nothing. Version 1 is shown exactly as the scanner saved it, and a scan lands in seconds, not minutes. A scan that goes in without its Google copy has no search text until the copy is built. A file with no text inside it that is added to Paperless some other way has no search text at all, because this app only looks after documents that carry `Physical Mail`. |
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
  - `daily_page_cap` stops runaway Google use. Scans may use all of it. Documents that are already stored may use three quarters of it.
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
- `/share/paperless/intake/documents/<Paperless document id>/`: one folder per document, holding `record.json` (what kind of file it is, whether it has its searchable version, which version of the app last worked on it, and the reason when nothing was done), `events.jsonl` (every change of that record) and `checks.json` (what was measured the last time a searchable version was made for it). Working files are removed when the work ends.
- `/share/paperless/intake/duplicates/` and `/share/paperless/intake/discarded/`: kept for 30 days.
