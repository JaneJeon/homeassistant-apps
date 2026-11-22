# Changelog

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
