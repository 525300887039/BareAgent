"""Stdio transport: subprocess + newline-delimited JSON.

Two background daemon threads consume the subprocess pipes:

- `_reader`: parses stdout as NDJSON, routes responses + notifications.
- `_stderr_reader`: drains stderr into the project logger so server banners
  do not block the pipe.

On EOF / subprocess death, every pending future is failed with
`MCPTransportError` so callers never hang forever.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Iterator
from typing import IO

from ..errors import MCPProtocolError, MCPTransportError
from ..protocol import Notification, Request, Response, decode_message
from .base import Transport

_log = logging.getLogger(__name__)

_TERMINATE_TIMEOUT = 2.0
_KILL_TIMEOUT = 2.0


class StdioTransport(Transport):
    """Spawn an MCP server as a subprocess and talk to it over stdin/stdout."""

    def __init__(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        super().__init__()
        if not command:
            raise ValueError("command must be a non-empty list")
        self._command = list(command)
        self._env = env
        self._cwd = cwd
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._closed = False
        # Set true by ``close()`` so the reader thread can distinguish graceful
        # shutdown (don't fire disconnect handler) from unexpected EOF.
        self._closing = False

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("StdioTransport already started")
        try:
            self._proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
                cwd=self._cwd,
                bufsize=0,
            )
        except (OSError, FileNotFoundError) as exc:
            raise MCPTransportError(f"failed to spawn MCP server: {exc}") from exc

        self._reader = threading.Thread(
            target=self._read_loop, name="mcp-stdio-reader", daemon=True
        )
        self._stderr_reader = threading.Thread(
            target=self._stderr_loop, name="mcp-stdio-stderr", daemon=True
        )
        self._reader.start()
        self._stderr_reader.start()

    def send(self, message: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPTransportError("transport not started")
        if proc.poll() is not None:
            raise MCPTransportError(f"subprocess exited with code {proc.returncode}")
        line = message + "\n"
        try:
            with self._write_lock:
                proc.stdin.write(line.encode("utf-8"))
                proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPTransportError(
                f"failed to write to subprocess stdin: {exc}"
            ) from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing = True
        proc = self._proc
        if proc is None:
            return
        # Graceful shutdown: close stdin → wait → terminate → wait → kill.
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                with self._write_lock:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
            try:
                proc.wait(timeout=_TERMINATE_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=_KILL_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=_KILL_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            self._fail_all_pending("stdio transport closed")

    def is_alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    # --- background threads ---

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        disconnect_reason: str | None = None
        try:
            for raw in self._iter_lines(proc.stdout):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = decode_message(line)
                except MCPProtocolError as exc:
                    _log.warning(
                        "MCP stdio: ignoring non-protocol line (%s): %r",
                        exc,
                        line[:200],
                    )
                    continue
                self._dispatch(msg)
        except Exception as exc:  # pragma: no cover — defensive
            disconnect_reason = f"stdio reader crashed: {exc}"
            _log.warning("MCP stdio reader crashed: %s", exc)
        finally:
            # Distinguish graceful (user called close) from unexpected EOF /
            # subprocess crash. Only fire disconnect handler in the unexpected
            # case so a normal shutdown does not spuriously notify the manager.
            if not self._closing:
                if disconnect_reason is None:
                    returncode = proc.poll()
                    if returncode is not None:
                        disconnect_reason = (
                            f"stdout EOF (subprocess exited with code {returncode})"
                        )
                    else:
                        disconnect_reason = "stdout EOF (subprocess exited)"
                self._invoke_disconnect(disconnect_reason)
            self._fail_all_pending("stdio reader exited (subprocess EOF or error)")

    def _stderr_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            for raw in self._iter_lines(proc.stderr):
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    _log.warning("MCP server stderr: %s", text)
        except Exception:  # pragma: no cover
            pass

    @staticmethod
    def _iter_lines(stream: IO[bytes]) -> Iterator[bytes]:
        # subprocess pipes opened in bytes mode iterate by line (\n-delimited).
        while True:
            line = stream.readline()
            if not line:
                return
            yield line

    def _dispatch(self, msg: Request | Response | Notification) -> None:
        if isinstance(msg, Response):
            self._route_response(msg)
        elif isinstance(msg, Notification):
            self._route_notification(msg)
        else:
            # Server-initiated request. PR1 does not support these; in PR2 the
            # client will handle method dispatch (ping, sampling, elicitation).
            _log.warning(
                "MCP stdio: ignoring server-to-client request (method=%r)", msg.method
            )
