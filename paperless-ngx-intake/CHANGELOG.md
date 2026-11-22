# Changelog

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
