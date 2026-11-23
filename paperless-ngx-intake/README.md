# Paperless Intake

Gives every scanned document in Paperless-ngx an upright, ordered, searchable version, read by Google Document AI. A scan from the scanner's folder is checked first, while you still have the paper: it is held, with an alert that names the problem, when a page looks missing, the file can't be read, or a day of retries failed. A document that reached Paperless any other way gets the same searchable version after the fact. See [DOCS.md](DOCS.md), and [DESIGN.md](DESIGN.md) for why it is built this way.

## How the pieces fit

```mermaid
flowchart TD
    scanner["Brother ADS-1800W scanner<br/>double-sided, drops blank pages"]
    doors["Any other way in:<br/>Paperless's web page, phone app, email,<br/>and everything stored before this app"]
    telegram["Telegram"]

    subgraph green["Home Assistant Green"]
        samba["Samba share app<br/>/share/paperless/capture"]

        subgraph before["Paperless Intake app, before storing: only a scan can be checked while you still have the paper"]
            make["Make the searchable version:<br/>Google Document AI reads the pages,<br/>the app turns them upright, puts them in order<br/>and lays the text on top"]
            gap{"Does a page<br/>look missing?"}
            held["Held on the Mail intake page<br/>until you keep or discard it"]
            store["Store both, then read back<br/>and compare"]
        end

        doc["Paperless-ngx app (stock): a document<br/>Version 1: the file as it arrived, untouched<br/>Newest version: the searchable one,<br/>which is what is shown and searched"]

        subgraph after["Paperless Intake app, after storing: the same question for every document"]
            need{"Was it scanned, and does it<br/>lack a searchable version?"}
            add["Make one the same way and<br/>add it as the newest version"]
            record["One record per document:<br/>done, not needed, gave up,<br/>or left for the next release"]
        end

        ha["Home Assistant automation<br/>Alert - Paperless intake"]
    end

    scanner -- "SMB" --> samba --> make --> gap
    gap -- "no" --> store
    gap -- "yes" --> held
    held -- "keep as it is" --> store
    store -- "REST" --> doc
    doors --> doc
    doc -- "asked every few minutes:<br/>what do you hold?" --> need
    need -- "yes" --> add --> record
    need -- "no" --> record
    held -. "alert" .-> ha -.-> telegram
```

Solid arrows are the path of a document. Dotted arrows are alerts. A stored scan can also raise an alert, only when something about it needs a look.

How a document arrived matters for one thing only: whether anything can be checked before it is stored. That is the top box. Once a document is in Paperless, the bottom box treats them all alike, and adds what it makes to the same document. Each document has one owner at a time: a scan belongs to the top box until it has finished going in, and to the bottom box from then on. The app works on one thing at a time, and scans always go first.

## Who does what

Paperless-ngx has its own OCR, page rotation and deskew. With Paperless's default settings they still run, but only on the untouched scan. With its OCR mode `off` they don't run at all. This table says whose result you actually see.

| Job | Whose result counts | What Paperless's own feature does |
| --- | --- | --- |
| Keeping the original | For a scan, this app uploads it as version 1 and checks it byte for byte. For any other document, version 1 is whatever Paperless was given, and this app never changes it | Stores it unchanged |
| Reading the text (OCR) | Google, through this app | With Paperless's defaults, Tesseract reads version 1 only. That text is superseded as soon as the copy is added. With Paperless's OCR mode `off`, nothing |
| Turning pages upright | This app, once per stack | With Paperless's defaults, rotation and deskew go into Paperless's own archive file of version 1, which is what it then shows as version 1. The copy already has text, so Paperless doesn't OCR it, rotate it or save it again |
| Page order | This app, from the printed page numbers | Nothing |
| Missing-page check | This app. A scan is held for it. For a document that is already stored it goes into the note | Nothing |
| Documents that didn't come from the scanner | This app asks Paperless every few minutes what it holds, and gives each scanned PDF it can see a searchable version. A PDF that already has text is left alone | Nothing. With its OCR mode `off`, Paperless reads nothing itself |
| Search text | Paperless reads it out of the copy with `pdftotext`. This app then compares it with Google's | |
| Document date | This app passes the day of the scan, from the scanner's file name | Not used. Paperless would guess a date from its own reading of version 1 |
| Duplicates | The same file dropped in the scanner's folder twice is stored once. A file that is already in Paperless by another route and then comes through the scanner's folder becomes a second document. The same letter scanned twice is two different files, and this app does nothing about that yet | Paperless 3 stores a file it already holds and flags both on its Duplicates tab |
| Alerts | This app, through a Home Assistant automation | Paperless workflows must not match `Mail intake` documents (see [DOCS.md](DOCS.md)) |

Two Paperless settings keep it this way. Both are in Paperless under Configuration, OCR Settings:

- **Mode** `auto` or `off`. Paperless's documentation says `redo` and `force` OCR every document again, which would replace Google's text in the copy.
- **Archive file generation** `auto` or `never`. With `always`, Paperless makes a PDF/A archive even of a PDF that has text, and shows its own re-saved file in place of the copy.

`auto` and `auto` are Paperless's defaults. `off` and `never` take Paperless out of the reading altogether: it stores and searches what this app gives it, and version 1 is shown exactly as the scanner saved it. The end-to-end tests run both ways, and once against `always` to prove the app notices. The app can't read the settings themselves. After every scan it checks what Paperless did, and the scan's alert says so if the copy got an archive file. See [DOCS.md](DOCS.md).

## Where each part is defined

| Part | Where it lives |
| --- | --- |
| This app's code and image | This folder. CI publishes `ghcr.io/janejeon/paperless-intake` |
| The top box of the picture | `pipeline.py`. One record per scan in `/share/paperless/intake/items/` |
| The bottom box of the picture | `documents.py`. One record per document in `/share/paperless/intake/documents/` |
| Adding a searchable version and reading it back | `searchable.py`, used by both boxes |
| This app's options | The app's Configuration tab in Home Assistant. Secrets are `!secret` references into `secrets.yaml` |
| The alert automation | `JaneJeon/homeassistant-config`, automation `Alert - Paperless intake` |
| The capture folder | `/share/paperless/capture`, shared by the Samba app |
| The scanner profile | On the scanner: double-sided, blank pages skipped, saved to the capture share |
| Paperless's OCR settings, tags, workflows and this app's user | Inside Paperless, in its database. Not in any repository |
