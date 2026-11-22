"""Google Document AI OCR, the decisions for the stack, and the searchable-copy renderer.

The Google backend and the page-number planner come from the October 2026
parser plugin (release 0.0.3), minus its Paperless glue. stack.py, glyphless.py
and the renderer's placement are from intake 1.1. Nothing here imports
Paperless. Importing never sends a document anywhere: only
GoogleDocumentAIBackend.process() makes network calls.
"""

__version__ = "0.1.0"
