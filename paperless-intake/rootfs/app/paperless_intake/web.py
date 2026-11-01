"""The Mail intake page, served only through Home Assistant ingress.

Requests are accepted only from Supervisor's ingress proxy, which has already
checked the Home Assistant login. Forms carry a per-process token as well.
"""

from __future__ import annotations

import html
import logging
import re
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import __version__
from .store import COMMITTED, COMMITTED_WARNING, FAILED, HELD, WAITING

log = logging.getLogger(__name__)
SHA = re.compile(r"[0-9a-f]{64}\Z")

STYLE = """
:root { color-scheme: light dark; --fg:#1c1c1c; --muted:#5f6368; --bg:#fafafa; --card:#fff; --line:#e0e0e0;
        --warn:#b3261e; --wait:#8a5a00; --ok:#1e6b37; --button:#03a9f4; }
@media (prefers-color-scheme: dark) { :root { --fg:#e1e1e1; --muted:#9aa0a6; --bg:#111; --card:#1c1c1c; --line:#333;
        --warn:#f2b8b5; --wait:#f5c26b; --ok:#8fd19e; } }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, sans-serif; }
main { max-width:760px; margin:0 auto; }
h1 { font-size:22px; margin:4px 0 4px; } h2 { font-size:17px; margin:24px 0 8px; }
.muted { color:var(--muted); font-size:13px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin:8px 0; }
.gap { margin:6px 0 10px; } .held .gap { color:var(--warn); } .waiting .gap { color:var(--wait); }
.row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
button, a.button { font:inherit; border-radius:8px; padding:6px 12px; border:1px solid var(--line); background:transparent;
        color:var(--fg); cursor:pointer; text-decoration:none; }
button.primary { background:var(--button); border-color:var(--button); color:#fff; }
.ok { color:var(--ok); }
"""


def _when(ts) -> str:
    if not ts:
        return "unknown time"
    return time.strftime("%b %-d, %-I:%M %p", time.localtime(float(ts)))


class Page:
    def __init__(self, service, pipeline, settings):
        self.service = service
        self.pipeline = pipeline
        self.settings = settings
        self.token = secrets.token_urlsafe(24)

    def render(self) -> str:
        items = self.service.store.all()
        held = [i for i in items if i.status == HELD]
        waiting = [i for i in items if i.status == WAITING]
        failed = [i for i in items if i.status == FAILED]
        done = [i for i in items if i.status in {COMMITTED, COMMITTED_WARNING}]
        done.sort(key=lambda i: i.state.get("committed_at_unix", 0), reverse=True)
        parts = [f"<main><h1>Mail intake</h1><p class='muted'>Scans are checked here before they go into Paperless. "
                 f"Paperless {html.escape(self.settings.tested_paperless_version)} is the tested version. "
                 f"paperless-intake {__version__}.</p>"]
        parts.append("<h2>Held: keep these papers out of the shred tray</h2>")
        parts += [self._card(i, "held") for i in held] or ["<p class='muted'>Nothing held.</p>"]
        parts.append("<h2>Waiting to retry</h2>")
        parts += [self._card(i, "waiting") for i in waiting] or ["<p class='muted'>Nothing waiting.</p>"]
        if failed:
            parts.append("<h2>Commit failed: keep the paper</h2>")
            parts += [self._card(i, "held") for i in failed]
        parts.append("<h2>Recently committed</h2>")
        if done:
            parts.append("<div class='card'>" + "<br>".join(
                f"{html.escape(i.state.get('original_name', ''))}: {i.state.get('page_count')} pages, "
                f"<span class='{'ok' if i.status == COMMITTED else 'gap'}'>"
                f"{'committed' if i.status == COMMITTED else 'committed with a warning: ' + html.escape(i.state.get('reason') or '')}"
                f"</span>, document {i.state.get('doc_id')}" for i in done[:15]) + "</div>")
        else:
            parts.append("<p class='muted'>Nothing yet.</p>")
        parts.append("</main>")
        return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
                f"initial-scale=1'><title>Mail intake</title><style>{STYLE}</style></head><body>{''.join(parts)}"
                f"</body></html>")

    def _card(self, item, css: str) -> str:
        sha = item.sha
        state = item.state
        name = html.escape(state.get("original_name", ""))
        when = _when(state.get("captured_at_unix"))
        gap = html.escape(state.get("reason") or "")
        buttons = [f"<a class='button' href='scan/{sha}' target='_blank' rel='noopener'>View scan</a>"]
        if item.status == HELD:
            buttons.append(self._form(sha, "keep_original", "Keep original only", primary=False))
            buttons.append(self._form(sha, "discard", "Discard, I'll rescan", primary=False))
        if item.status in {HELD, WAITING} and self.pipeline.can_retry(item):
            buttons.append(self._form(sha, "retry", "Retry now", primary=True))
        if item.status == WAITING:
            buttons.append(self._form(sha, "discard", "Discard, I'll rescan", primary=False))
            gap += f"<br><span class='muted'>Next try {_when(state.get('next_attempt_at_unix'))}.</span>"
        return (f"<div class='card {css}'><strong>Scanned {when}</strong>, {state.get('page_count') or '?'} pages "
                f"<span class='muted'>({name})</span><div class='gap'>{gap}</div>"
                f"<div class='row'>{''.join(buttons)}</div></div>")

    def _form(self, sha: str, action: str, label: str, *, primary: bool) -> str:
        return (f"<form method='post' action='action/{sha}/{action}'><input type='hidden' name='token' "
                f"value='{self.token}'><button class='{'primary' if primary else ''}' type='submit'>{label}</button></form>")

    def act(self, sha: str, action: str) -> None:
        with self.service.lock:
            item = self.service.store.load(sha)
            if action == "keep_original":
                self.pipeline.keep_original_only(item)
            elif action == "discard":
                self.pipeline.discard(item)
            elif action == "retry":
                self.pipeline.retry(item)
            else:
                raise ValueError("unknown action")
            log.info("Jane chose %s for %s", action, sha[:12])


def make_handler(page: Page, allowed_clients: tuple[str, ...]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "paperless-intake"

        def log_message(self, fmt, *args):  # keep request lines out of the app log
            log.debug("web: " + fmt, *args)

        def _allowed(self) -> bool:
            if self.client_address[0] in allowed_clients:
                return True
            self.send_error(HTTPStatus.FORBIDDEN)
            return False

        def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _base(self) -> str:
            return (self.headers.get("X-Ingress-Path") or "").rstrip("/") + "/"

        def do_GET(self):
            if not self._allowed():
                return
            path = self.path.split("?", 1)[0]
            if path in {"/", ""}:
                self._send(200, page.render().encode(), "text/html; charset=utf-8")
                return
            match = re.fullmatch(r"/scan/([0-9a-f]{64})", path)
            if match and page.service.store.exists(match.group(1)):
                source = page.service.store.load(match.group(1)).source_path
                if source.exists():
                    self._send(200, source.read_bytes(), "application/pdf",
                               {"Content-Disposition": "inline; filename=scan.pdf"})
                    return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self):
            if not self._allowed():
                return
            match = re.fullmatch(r"/action/([0-9a-f]{64})/(keep_original|discard|retry)", self.path)
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            if not match or form.get("token", [""])[0] != page.token:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            try:
                page.act(match.group(1), match.group(2))
            except (ValueError, FileNotFoundError) as error:
                self._send(409, f"Not done: {html.escape(str(error))}".encode(), "text/plain; charset=utf-8")
                return
            self._send(303, b"", "text/plain", {"Location": self._base()})

    return Handler


def serve(page: Page, port: int, allowed_clients: tuple[str, ...]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(page, allowed_clients))
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    return server
