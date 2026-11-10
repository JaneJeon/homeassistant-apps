"""Google Document AI OCR, page planning and the searchable-copy renderer.

These modules come from the October 2026 parser plugin (release 0.0.3), minus
its Paperless glue. Nothing here imports Paperless. Importing never sends a
document anywhere: only GoogleDocumentAIBackend.process() makes network calls.
"""

__version__ = "0.0.3"
