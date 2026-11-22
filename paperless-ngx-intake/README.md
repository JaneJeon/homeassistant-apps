# Paperless Intake

Takes scans from the scanner's folder, makes an upright, ordered, searchable copy with Google Document AI OCR, and commits the untouched scan plus that copy into stock Paperless-ngx through its REST API. A scan is held, with an alert that names the problem, when a page looks missing, the file can't be read, or a day of retries failed. See [DOCS.md](DOCS.md).
