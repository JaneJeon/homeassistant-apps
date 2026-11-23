# Design: every document gets the same treatment, however it arrived

Written 2026-11-23, with release 1.3.0. It went through two independent reviews before any of it was built. The last section says what they changed.

Words used here:

- **Scan:** a PDF from the scanner's folder that is not in Paperless yet.
- **Document:** something that is in Paperless.
- **File version:** Paperless keeps a history of files for each document. Version 1 is the first file. The newest version is the one Paperless shows and searches.
- **Searchable version:** the file the app makes: the same page images turned upright and put in order, with Google's text laid invisibly on top.
- **Release:** a version of the app (1.2.0, 1.3.0).

## What it has to do

- Google is the only thing that reads a scan. Paperless's own OCR does not run.
- The app takes care of everything in Paperless, not only what comes out of the scanner.
- Each piece of work has one owner. Nothing is done twice, and nothing is missed after a restart.
- It can be explained with one picture.
- Later steps fit without a rebuild: email as another way in, dates and people, duplicates. A document that was not scanned needs none of the scan steps.

## The gap before 1.3.0

The app only saw the scanner's folder. Paperless has other ways in: its web page, the phone app, email, its own consume folder. And 46 mail documents were stored before the app existed. None of those ever met the app. With Paperless's OCR off, a scan that arrives one of those ways has no search text at all.

## The idea: the document is the unit, not the route

How a document arrived matters for exactly one thing: **whether anything can be checked before it is stored.** Only the scanner's folder allows that, and it matters there because the paper is still in hand.

Once a document is in Paperless, the route no longer matters. Every document gets the same question: **was it scanned, and does it lack a searchable version?**

```mermaid
flowchart TD
    scanner["Scanner's folder"] --> check{"Before storing:<br/>Google reads it.<br/>Readable? Page missing?"}
    check -- "a page may be missing" --> held["Held. Keep or discard.<br/>The paper is still in hand"]
    held -- "keep" --> store
    check -- "fine" --> store["Store the untouched scan as version 1<br/>and the searchable version as the newest"]
    doors["Paperless's web page, phone app,<br/>email, consume folder,<br/>and everything stored earlier"] --> stored
    store --> stored["A document in Paperless"]
    stored --> need{"Was it scanned,<br/>and does it lack a<br/>searchable version?"}
    need -- "yes" --> make["Make the searchable version<br/>(Google's reading, upright, in order).<br/>Add it as the newest version, with a note"]
    need -- "no: it has its own text,<br/>or it already has one" --> none["Nothing to do"]
    make --> record["One record per document:<br/>what was done, by which release,<br/>or why not"]
    none --> record
```

The top half is `pipeline.py`. The bottom half is `documents.py`. The 46 old documents simply start at "A document in Paperless".

## Who owns a document

One rule, so that no document is ever worked on from two sides:

> A document belongs to its scan until that scan has finished going in and has written the document's record. From then on it belongs to the loop.

The loop recognises "an unfinished scan is putting this document in" by the file: version 1 of the document is the file of a scan record that has not finished. It leaves such a document alone. When the scan finishes, the last thing it does is write the document's record, from what really happened: done, redo (the searchable version reads back badly, or the app could not build one), not needed (almost no text), or gave up (Google refused that scan).

There is no lock file. Home Assistant runs exactly one container for the app, the app is that container's first process, and it works on one thing at a time.

## How the bottom half works

**One record per document**, in a folder named after the document's id in Paperless. It says what kind of file version 1 is, whether the document has its searchable version, which release last worked on it, and if nothing was done, why. A second file keeps every change of that record. Later steps (dates, people) can add their own lines to the same record.

**One loop, one document at a time.** The app already had a single loop that works one scan at a time. When no scan is waiting, the same loop does one round of this:

1. If a document's work was cut short, finish it.
2. Otherwise take one that is due another try, then one a newer release should redo.
3. Otherwise ask Paperless for its list of documents (at most every five minutes) and take the first one that has no record.
4. Look at its version 1 and decide the kind:
   - **Scanned:** a PDF with no text of its own inside the file. Tested on the file itself. Paperless's text field is no guide, because for old documents it holds Tesseract's reading.
   - **Has its own text:** made on a computer, or read before it was stored. Recorded as "not needed", for good.
   - **Not a PDF:** a photo. Recorded as "not yet". A release that can read images picks these up.
5. For a scanned one: Google reads it, the app decides upright and order for the stack, builds the searchable version, adds it to the document, reads it back, writes a note, and records "done".

**If it stops halfway**, it starts that document again from the top and skips what is already done. Each step looks first: is the file here, is the searchable version built, does the document already have it, is the note there. So a step that runs twice does its work once.

**What it may change:** it adds one file version and one note. It adds a version only when every other file version on the document is one the app added itself. So it can improve its own earlier work, and it stays away from a document somebody else has given a file version. It never touches owner, tags, date or title, and it never deletes.

**What it never does for a stored document:** hold, ask or alert. The document is already stored. A page that looks missing goes into the note.

**When something is in the way:**

- Outside the document (Google or Paperless not answering, disk low, the day's Google page limit): try again in an hour, for up to a day. Then leave it until the next release.
- The document itself (Google cannot read it): give up, with the reason recorded.
- The app itself (it cannot build the version, or an error nobody planned for): three tries at most, then leave it until the next release.

**Each release tries once** what an earlier release could not do. That is the whole of "re-do after an update".

## Where the truth is kept

The documents themselves are the truth. The records are the app's notes about them. Three consequences:

1. **The to-do list is worked out, not stored.** There is no queue to lose. "Paperless holds a document the app has no record of" is found by asking Paperless, every time.
2. **The loop asks; it does not wait to be told.** A webhook can be missed while the app is down. Asking again finds everything. The cost is a few minutes' delay.
3. **If the app's records were ever lost, finished work would not be done again.** A document whose newest file version carries the app's label already has its searchable version, and the loop records that without asking Google again.

paperless-gpt, the best-known tool for this job, keeps its to-do list as tags inside Paperless, and its issue tracker shows the tags and reality drifting apart. That is the alternative this design avoids.

## What this replaced

Before 1.3.0, re-doing a searchable version for a scan that was already stored was handled by flags on the scan's record and a saved snapshot to roll back to: the scan's record was moved backwards through its own states. Most of the defects the code reviews found on 2026-11-22 were in that code. It also only worked while the local scan was kept (7 days).

That code is deleted. The same need is met by the record: "has a searchable version that reads badly" or "has none, and the fault was the app's" means the next release tries again. The file is fetched from Paperless, so the 7-day limit is gone.

## Premortem: a month from now this went badly. Why?

| What went wrong | What prevents it |
| --- | --- |
| The searchable versions were worse than what was there, across 46 documents | Version 1 is never touched, and an added version can be deleted in Paperless. Release 1.3.0 starts on three documents only. They are looked at by eye before the next release lifts the limit |
| A document got two searchable versions | Before adding, the app asks Paperless whether the document already has that exact file. Tests stop the app at each step and start it again |
| Two parts of the app worked on one document | The ownership rule above, with tests for a scan that is half way in |
| One bad document looped and used up Google pages | Tries are counted and capped. Google's answer is kept until the work ends, so a retry does not pay twice. This work stops at three quarters of the day's page limit, so scans always have room |
| Real scans were delayed | Scans go first. One document at a time |
| A document that does not fit today's page allowance blocked the ones behind it | The loop passes over it and carries on with smaller ones. One too big for any day is set aside with the reason |
| Hand uploads were not covered and Jane assumed they were | The app's Paperless user cannot see a document that somebody owns, and an upload on the web page belongs to the uploader. DOCS.md says so and says how to share them. The 46 old documents have no owner, so they are visible |
| A Paperless update changed how file versions work | Same rule as for scans: nothing is written unless Paperless is at a version the tests passed on |
| Jane changed a document while it was being worked on | Her tags, owner and date are never written. A file version she added makes the app leave the document alone. If the document is gone, the record says so and nothing else happens |
| The Green ran out of disk | Working files are deleted when a document is finished. Each document grows by one file version, roughly its own size again. Work pauses when free space is under the app's limit |
| A page was missing from a hand upload and nobody noticed | Known weak spot. It is written in the note, with no alert. Left for Jane to decide |

## What comes next, and how it fits

- **Photos.** A new kind in step 4. Records marked "not yet" are picked up by the release that adds it.
- **Email.** Another way in, on the "Paperless's web page, phone app, email" side of the picture. An attachment that has its own text needs nothing. A scanned one is found by the loop like any other, as long as the app's user can see it.
- **Dates and people.** More steps after "does it have a searchable version", writing to the same record.
- **Duplicates.** Its own research and design first. Nothing that deletes or merges is built without Jane.

## What the two reviews changed

First review:

- The first draft kept scans and stored documents as two separate kinds of record and left the old flags in place "for now". The reviewer's main point was that the document should be the unit and the flags should go now. Adopted.
- "Left" meant both "cannot" and "does not need". Split into "not needed" (for good) and "gave up" (until the next release, when the fault was the app's).
- A "first three, then press Continue" button was a permanent control for a one-time rollout. Replaced by a limit in one release that the next release removes.
- A lock file was planned. Dropped, for the reason given under "Who owns a document".
- The words "queue", "producer" and "consumer" promised more than is there. It is one loop and a record per document.

Second review:

- The ownership rule had to be written down as one sentence and tested, because a scan that is half way in is exactly where two workers could meet.
- The loop was going to remember the highest document id it had seen. Dropped. Asking for the whole list each time is cheap at this size and cannot skip anything.
- A scan's document record must say what really happened to the scan, not assume "done".
- "Add a version only while the document has exactly one" would have stopped the app improving its own earlier work. Replaced by "only when every other file version is the app's own".
