from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from queue import Empty
from socketserver import ThreadingMixIn
import threading
from typing import Any
from urllib.parse import unquote, urlparse

_VIEWER_HTML = Path(__file__).with_name("viewer.html")


class DebugViewerHandler(BaseHTTPRequestHandler):
    """Serve the debug viewer SPA and read-only interaction APIs."""

    server: DebugViewerServer

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default HTTP request logging."""

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/":
            self._serve_html()
            return

        if path == "/api/sessions":
            self._serve_json(self.server.logger.list_sessions())
            return

        session_prefix = "/api/sessions/"
        if path.startswith(session_prefix):
            session_id = unquote(path.removeprefix(session_prefix))
            if session_id and "/" not in session_id:
                try:
                    self._serve_json(self.server.logger.list_interactions(session_id))
                except ValueError:
                    self._serve_error(404, "Not Found")
                return

        interaction_prefix = "/api/interactions/"
        if path.startswith(interaction_prefix):
            remainder = path.removeprefix(interaction_prefix)
            parts = [unquote(part) for part in remainder.split("/", maxsplit=1)]
            if len(parts) == 2 and parts[0]:
                session_id, seq_text = parts
                try:
                    seq = int(seq_text)
                except ValueError:
                    self._serve_error(400, "Invalid seq")
                    return
                try:
                    self._serve_json(
                        self.server.logger.get_interaction(session_id, seq)
                    )
                except ValueError:
                    self._serve_error(404, "Not Found")
                return

        if path == "/api/events":
            self._serve_sse()
            return

        self._serve_error(404, "Not Found")

    def _serve_html(self) -> None:
        try:
            content = _VIEWER_HTML.read_bytes()
        except OSError:
            self._serve_error(500, "viewer.html not found")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_json(self, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_error(self, code: int, message: str) -> None:
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        subscribe = getattr(self.server.logger, "subscribe_events", None)
        unsubscribe = getattr(self.server.logger, "unsubscribe_events", None)
        event_queue = (
            subscribe() if callable(subscribe) else self.server.logger.event_queue
        )

        try:
            while True:
                try:
                    event = event_queue.get(timeout=30)
                    payload = json.dumps(event, ensure_ascii=False, default=str)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                except Empty:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            if callable(unsubscribe):
                unsubscribe(event_queue)


class DebugViewerServer(ThreadingMixIn, HTTPServer):
    """HTTP server that exposes an attached interaction logger."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        logger: Any,
        port: int = 8321,
        host: str = "127.0.0.1",
    ) -> None:
        self.logger = logger
        super().__init__((host, port), DebugViewerHandler)


def start_viewer(
    logger: Any,
    port: int = 8321,
    host: str = "127.0.0.1",
) -> tuple[DebugViewerServer, threading.Thread]:
    """Start the debug viewer on a daemon thread."""

    server = DebugViewerServer(logger, port=port, host=host)
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="debug-viewer",
    )
    thread.start()
    return server, thread
