# Paperless-ngx MCP

This app runs the pinned [nloui/paperless-mcp](https://github.com/nloui/paperless-mcp) source at commit `4ba74570e6161b955fd1f3071ce9472c5b689295` in its own container. Its MCP endpoint is `http://homeassistant:18083/mcp` for both Codex Desktop and Claude Desktop. Claude Desktop uses the local `fastmcp-remote@4.0.7` bridge to that exact URL.

## Options

- **Paperless URL** defaults to the existing app's internal address, `http://ca5234a0-paperless-ngx:80`.
- **API token** is required. Use a token belonging to a dedicated Paperless account with view permission for documents, tags, correspondents, and document types, and access to the documents that should be searchable. Enter it in this app's password Option. The app will not start while the placeholder value remains.

Paperless-ngx, OCR, scanner ingestion, and archive storage are not configured by this app. The API token is supplied to the server at runtime; it is not included in the image, repository, or desktop client configuration. Rotate it in Paperless and replace the app Option if needed.

## Read-only surface

The app exposes the six retrieval tools present in the pinned upstream source: `search_documents`, `get_document`, `download_document`, `list_tags`, `list_correspondents`, and `list_document_types`. The upstream write tools are not registered, including upload, bulk edits, and metadata creation. `download_document` supports the retained original and the archive derivative. The MCP endpoint has no separate caller authentication, matching the other local MCP apps. Keep host port 18083 reachable only on the home LAN and tailnet.

## Source adjustments

The Docker build verifies the pinned upstream source archive, applies the reviewed `rootfs/upstream.patch`, and installs the checked-in `rootfs/package-lock.json` with `npm ci`. That lockfile updates vulnerable dependencies within upstream's declared version ranges and passed `npm audit` when packaged. The patch gives each Streamable HTTP request its own MCP server instance, registers only the six read tools, and requests Paperless API version 10 instead of the upstream version 5 header. It serves only `/mcp`; legacy SSE is unnecessary for the desktop clients.
