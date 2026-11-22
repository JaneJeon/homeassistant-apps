# Changelog

## 1.1.0

Version 1.0 held both of the first two real scans it was given, over the invisible text layer. Neither had lost anything. This version changes what can hold a scan.

- **Only a missing page or an unreadable file holds a scan now.** Rotation, page order, text position and scripts the old font lacked are decided, written into the document's note, and never hold. Scans that 1.0 held for those reasons are put through again when this version starts.
- **Rotation and order are decided for the whole stack.** A page with little text is turned like the rest of its stack. A flipped stack is reversed whole when the page numbers that could be read run backwards, not only when every page is numbered.
- **A page a degree or two askew goes through.** 1.0 held anything more than half a degree off.
- **The text layer can carry any script.** It uses a glyphless font, the technique Tesseract and OCRmyPDF use, placed word by word. Korean, Chinese, Thai and the like go in and search.
- **An unread page number is no longer taken for a missing page** when the scan has an unnumbered page that could be carrying it.
- **"Keep original only" is now "Keep as it is".** A scan held for a missing page already has its searchable copy, so keeping it stores both versions.
- **If Google can't read one scan,** the untouched scan still goes into Paperless and an alert says so. If Google refuses the setup, scans wait and an alert says why.
- The read-back compares searchable words, so punctuation spacing can't raise a false warning.
- Removed the reportlab dependency and the DejaVu font package.
