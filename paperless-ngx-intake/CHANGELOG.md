# Changelog

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
