# Changelog

## 1.4.0

The app is now called **Paperless-ngx Scanner**, and its tag **Scanner**. It used to be called Paperless Intake, and the tag Physical Mail. The slug, the image and the folder are unchanged. The sidebar page is now **Scans**.

- **A job queue at the core.** The records on disk are the queue: one job at a time, new scans first. A job is leased to its worker with the service's run id. A job found in hand after the app was killed is resumed, and that is not a try. Only a worker death the running app saw counts against the job. A job that takes the whole app down ten times at the same step is set aside.
- **Nothing starts when it shouldn't.** Before a job starts, the queue checks the Green and Paperless. No job starts while an upload whose outcome isn't known may still be on its way into Paperless (the night of 2026-11-22, uploads that timed out landed minutes later). None starts while available memory is under `min_available_mib` (new, 500 MiB), free disk is under `min_free_mib` (checked here now, not inside the worker), or the last minute's disk I/O or memory stalls reach `max_io_pressure` (new, 60%) or `max_memory_pressure` (new, 40%). These start high until the Green's real figures are known: the log has one line an hour of both readings and available memory. A new scan held back waits two minutes at a time, with the reason on the Scans page. It isn't a failed try, and after a day it is held with an alert, as before.
- **An upload in doubt is always settled.** It landed (by checksum), its task ended, or it never landed: its task stayed running past Paperless's own time limit (new option `paperless_worker_timeout`, 1800 s like Paperless's) and 15 minutes more after it started, or queued for six hours, or Paperless has no unfinished upload from after it. A file that never lands three times sets its job aside, a new scan with an alert. The Scans page has a "Forget this upload" button.
- **Stored scans don't pile onto a struggling Paperless.** While one waits on Paperless or Google, no other starts: its own next try is the check. Two minutes pass between stored scans. Uploads now wait up to 10 minutes for Paperless's answer.
- **Failures are told apart.** A refused connection, a timeout, a 502/503/504 or a refused login is about Paperless itself. A 404 means the document is gone. Anything else is about that document, and gets three tries.
- **Killed at any moment, every job still ends once.** Each step looks before it acts. An intent mark goes down before every upload, so a file is never sent again while Paperless may still be taking it in. Google's pages are counted once per answer. "started" and "finished" go to the watchdog at most once each, and "finished" goes out however many restarts it took. Alerts are written into the record with the change they are about, and sent until Home Assistant takes them. A claim cut short between the capture folder and its record is finished or undone at start. New tests kill the app, and separately its worker, at every step, and check the end state.
- **The app's own numbers in Home Assistant**, over MQTT with discovery, as the Paperless-ngx Scanner device: Status, Paused, Paperless reachable, Scans held, Scans retrying, Scans failed, Last scan, Google pages today, Google pages this month, Stored scans to re-read, Stored scans set aside. DOCS.md says what each means. The broker comes from the Supervisor (the manifest asks for the MQTT service). Without one, the app works as before.
- **"Today" is the local day**, for the page cap too: the Supervisor gives the app Home Assistant's time zone. It was the UTC day.
- **Options.** `scanner_tag` (default `Scanner`) replaces `physical_mail_tag`. The Supervisor drops the old stored option with a warning in its log, so nothing needs doing. `intake_tag` defaults to `Mail Intake`; the app finds tags by name whatever their case. The webhook ids have no defaults: an empty one means don't send, and the ones already set are kept.
- **A missing tag is a wait, never a crash.** Rename `Physical Mail` to `Scanner` in Paperless once this version is in. Until then new scans wait with one alert, and stored scans wait.
- On the Supervisor's stop, the app stops its worker at once and exits within a second. Nothing depends on it: the job is resumed either way.
- README, DOCS and DESIGN describe the queue as stages that can be checked from outside, with jobs as the steps between them, and name this app's part of the document system: the scan step, plus the ingest check for what the scanner produces.

## 1.3.2

- **The three-scan limit is gone.** Version 1.3.1 started on three stored scans so the result could be looked at first. They were looked at in Paperless: upright, in order, a note on each, the original still version 1, and nothing else about them changed. The rest of the scans stored before the app are now re-read, one at a time, within three quarters of the day's Google page cap.
- The code for the limit is removed, not switched off.
- Small corrections to README, DOCS and DESIGN.

## 1.3.1

Version 1.3.0 was never installed. Before it went in, what the app looks at was cut back to what the app is for.

- **Scanner only.** The app looks after what comes out of the scanner, and nothing else. Version 1.3.0 looked at every document it could see and judged from the file whether it had been scanned. That reached into kinds of document the app was never tested on.
- **The `Physical Mail` tag is the boundary, and the only test.** The app takes on a stored document only if it carries the tag. The app puts the tag on every scan it stores, and a Paperless rule put it on the scans stored before the app. A document without the tag is not fetched, read or added to.
- **A scan that is missing the tag is mended on the document:** add the tag in Paperless, and the app takes it on at its next look. This release starts on only three stored scans, so after those a newly tagged one waits for the next release, and nothing new is started while any document waits for tomorrow's Google pages. The app has no second way of recognising a scan, such as the file's name.
- **The tag is checked every time the app is about to work on a stored document**, not only when it first meets it. If the tag has been taken off, the app does nothing to the document and lets it go. If the tag comes back, the app takes it on again.
- **The app asks Paperless only for documents that carry the tag.** Paperless sends it nothing else, and the app still checks the tag on each document it is sent.
- **A tagged file that isn't a PDF is set aside for good**, with the reason on the Mail intake page. In 1.3.0 it waited for "a version that can read images". The scanner saves PDFs, and giving a photo the tag must not be a way to get it read.
- Known and accepted: with Paperless's own OCR off, a file with no text inside it that reaches Paperless another way has no search text. It can be found by the missing tag.
- As planned for 1.3.0, this version starts on three scans stored before the app, so the result can be looked at first. The next version removes the limit.
- The Mail intake page, the notes, README, DOCS and DESIGN say all this. README and DESIGN have a section "Where the app stops".
- Tests: a document without the tag, in plain sight of the app, is never looked at, and is taken on once it is given the tag. Both in the unit tests and against real Paperless.

Everything else listed under 1.3.0 below is in this version unchanged, except where 1.3.0 says "every document" or "however it arrived".

## 1.3.0

Until now the app only saw the scanner's folder. Anything that reached Paperless another way never met it, and with Paperless's own OCR switched off such a document had no search text at all. The same went for everything stored before the app existed.

- **Every document in Paperless is looked after, however it arrived.** Every five minutes the app asks Paperless what it holds. A scanned PDF it hasn't met gets a searchable version made the same way as for a scan, added as the newest version with a note. A PDF that already has text is left exactly as it is. A file that isn't a PDF waits for a version that can read images. Version 1 of a document is never changed, nor its owner, tags, date or title.
- **One record per document** says what was done, by which version of the app, or why nothing was. Every document ends as done, not needed, gave up, or left for the next version of the app. The Mail intake page lists them.
- **A stored document is never held and never alerts.** A page that looks missing goes into its note.
- **Re-doing a searchable copy is the same loop.** A copy Paperless couldn't read all the words of, or one the app failed to build, is tried once by each new version of the app, working from version 1 as Paperless holds it. It no longer depends on the local scan still being kept. The old way of doing this (moving the scan's own record backwards with a snapshot to restore) is gone, and scan records left in that state are put back as they were.
- **One owner at a time.** A scan's document belongs to the scan until the scan has finished going in, and to the loop from then on. The loop leaves alone any document that an unfinished scan is still putting in, and any document with a file version somebody else added.
- **A scan takes for its own only a document the app stored.** Before, a scan whose file was already in Paperless by another route adopted that document, cleared its owner and added to it. Now the scan becomes a document of its own, and Paperless flags the two as duplicates.
- **A scan cut short just after its note was written no longer gets a second note.** The app looks for the note before writing it.
- **Scans go first, and keep room.** Stored documents are worked on one at a time, only when no scan is waiting, and use at most three quarters of the day's Google page cap. One that doesn't fit today waits for tomorrow.
- **This version starts on three documents only** from outside the scanner's folder, so the result can be looked at in Paperless before the rest follow. The next version removes the limit.
- A document the app's Paperless user can't see (one that has an owner, such as a web upload) is not covered. DOCS.md says how to share such documents with the app.
- New end-to-end tests: a raw scan from Paperless's own consume folder gets its searchable version with nothing else about it changed and no ingest webhook fired; a PDF with text is left alone; an owned document is invisible until shared; a scan that is half way in is left to the scan; the same file by two routes is two documents.

## 1.2.0

Google is meant to be the only reader. Paperless still read every raw scan itself with Tesseract, and that second reading showed in two places: the document's date, and what Paperless displays as version 1 (its own turned and cleaned rendition, not the scan). This version makes the app ready for Paperless with its own OCR switched off.

- **The document's date is the day it was scanned.** The app takes it from the scanner's file name, or uses the day it picked the file up, and gives it to Paperless with the upload. Paperless no longer guesses a date from its own reading of the raw scan.
- **Works with Paperless's OCR mode `off` and archive file generation `never`.** The end-to-end tests now run against Paperless set that way as well as against its defaults. A scan that goes in without its Google copy then has no search text, and its note says that instead of saying Paperless did its own OCR.
- **The warning for Paperless re-saving the searchable copy says what happened and where to look.** The check itself is not new: after every scan the app looks for an archive file on the copy. The tests now also run against Paperless set to archive every PDF, to prove the warning is sent.
- README: a diagram of the whole path, and tables of who does what and where each part is defined.

## 1.1.3

Version 1.1.2 put both real scans into Paperless upright and in page order. Both came with a warning that about one word in twenty was missing from Paperless's search text.

- **Neighbouring words no longer run together in Paperless's text.** Paperless reads the text with pdftotext, which ignores a space character and decides where words end by the gap it measures. Google's boxes for neighbouring words often touch, and then two words came out as one ("En Español" as "EnEspañol"). Each word that a space follows now keeps a space cell inside its own box, as Tesseract's PDF output does.
- **A stored copy that Paperless couldn't read all the words of is built again** by the next version of the app, once, and added to the same document as a newer version with a second note. The earlier warning is closed if the new copy reads back well. If the try fails, everything recorded about the stored copy is put back.

## 1.1.2

Version 1.1.1 put both waiting real scans through. Looking at the results showed four more things.

- **Page numbers are read on a stack that went in upside down.** The first real scan went in turned and flipped. Its pages came out upright and in reverse, "Page 6 of 6" first, and the note said the order was unverified. Google had labelled the lines of each turned page one way and the page another, and the app threw every page number away. It now goes by how most of a page's lines run.
- **A nearly empty page follows the stack even when Google reads it as sideways.** The second real scan ended with a back page that had one word on it, which Google read as turned a quarter. One word is not enough to turn a page on.
- **Page numbers printed at the top of some pages and the bottom of others count as one series** for the missing-page check, when they count to the same total and no number is printed in both places. Kept apart, each place looked as if it had gaps. A number printed in both places means two documents, and each is still checked by itself.
- **The image-match remark is made only when the match is plainly wrong** (below 0.6). Pages whose text sat exactly on the printed words, checked by eye in Paperless, scored 0.83 to 0.999, so the old 0.985 yardstick remarked on good pages.
- **A scan that hasn't finished going into Paperless is prepared again by a new version** of the app. It costs nothing, because Google's answer is still saved.
- The log line for each page now says how Google labelled the page and its lines.

## 1.1.1

The first real scan 1.1.0 worked on went in without its searchable copy. The scanner had saved some of its pages as one-bit black and white images and the rest as JPEGs, and 1.1.0 built a copy only from JPEG pages. No test scan had a page like that.

- **The copy is built from any page image.** JPEG, one-bit fax or Flate images, JBIG2, a page in layers: the page is carried over as the scanner wrote it and the text is laid on top. Nothing needs to decode the image. The check that every image is the scanner's own, byte for byte, now covers every image on the page, with the description that says how its bytes are read.
- **A copy an earlier version couldn't build is built again.** When a new version starts, it tries once more for each scan whose copy failed in the app's own handling, for as long as the local scan is kept. A scan still on its way into Paperless carries on with the copy. A scan already in Paperless gets the copy added as version 2 of the same document, with a second note, and the earlier warning is closed. If the try fails, the document stays exactly as it was: never held, never waiting, nothing new said. Its owner and tags are left as Jane has them. Three tries per version at most.
- **When a copy can't be built, the log records the shape of each page** as the scanner wrote it (page box, image formats and sizes, drawing operators), so the cause can be read without opening the scan.
- **The log names each page's image format** next to its turn, word count and image match. The image match is left unmeasured, with the reason, for a page the app can't decode or that isn't a single image. Only JPEG pages are remarked on against the match yardstick, because that is what it was set on.

## 1.1.0

Version 1.0 held both of the first two real scans it was given, over the invisible text layer. Neither had lost anything. This version changes what can hold a scan.

- **A scan is held for a missing page, an unreadable file, or a day of failed retries, and nothing else.** Rotation, page order, text position and scripts the old font lacked are decided, written into the document's note, and never hold. Scans that 1.0 held for those reasons are put through again when this version starts.
- **Rotation and order are decided for the whole stack.** Each page with plenty of text gets one vote on which way up the stack is. A page with little text is turned like the rest of its stack. A flipped stack is reversed whole when the page numbers that could be read run backwards, not only when every page is numbered.
- **A page a degree or two askew goes through.** 1.0 held anything more than half a degree off.
- **The text layer no longer depends on a font having the right letters.** It uses a glyphless font, the technique Tesseract and OCRmyPDF use, placed word by word. Korean, Chinese, Thai and the like go in and search. Arabic and Hebrew are stored so they read back in order. Characters outside Unicode's basic plane are left out.
- **A first page with no number on it is no longer taken for a missing page.** When the only number unaccounted for is 1 and the scan has an unnumbered page, the scan goes through. Every other gap in a printed series still holds.
- **A page number that is only referred to** ("see page 4") is no longer read as that page's number.
- **"Keep original only" is now "Keep as it is".** A scan held for a missing page already has its searchable copy, so keeping it stores both versions.
- **If Google can't read one scan, or the copy can't be built,** the untouched scan still goes into Paperless and an alert says so. If Google refuses the setup, scans wait and an alert says why.
- **An unexpected error in the app sends one alert straight away.** 1.0 retried quietly for a day first.
- The read-back compares searchable words, so punctuation spacing can't raise a false warning.
- Removed the reportlab dependency and the DejaVu font package.
