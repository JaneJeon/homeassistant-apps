"""Source-bound cached responses for offline trials; contains no fixture data."""

import json
from pathlib import Path

from .errors import IntegrityError, ResultValidationError
from .types import OCRChunk, ProcessingResult, SourcePDF
from .validation import sha256, validate_result


class CachedResponseBackend:
    """Explicit manifest: source_sha256 plus chunks with response_path/source_pages.

    response_path values resolve relative to the manifest. Each chunk must also
    name response_sha256 and processor_version. Raw response files are never
    rewritten. This is deliberately not a network backend or an inferred cache.
    """

    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path.resolve(strict=True)

    def process(self, source: SourcePDF, work_dir: Path) -> ProcessingResult:
        manifest_bytes = self.manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        if manifest.get("source_sha256") != source.sha256:
            raise IntegrityError("Cached OCR is bound to a different source SHA-256.")
        chunks = []
        for entry in manifest.get("chunks", []):
            response_path = (self.manifest_path.parent / entry["response_path"]).resolve(strict=True)
            raw = response_path.read_bytes()
            response_hash = sha256(raw)
            if response_hash != entry["response_sha256"]:
                raise IntegrityError("Cached OCR response digest changed.")
            response = json.loads(raw)
            if "document" not in response:
                raise ResultValidationError("Cached response is missing its Google document.")
            chunks.append(OCRChunk(source_pages=tuple(entry["source_pages"]), document=response["document"],
                                   processor_version=entry["processor_version"], response_sha256=response_hash,
                                   provenance={"response_path": str(response_path), "cached": True}))
        result = ProcessingResult(tuple(chunks), provenance={"backend": "explicit_cached_responses", "cache_manifest_path": str(self.manifest_path),
                                                            "cache_manifest_sha256": sha256(manifest_bytes), "network_requests": 0})
        validate_result(result, source)
        return result

