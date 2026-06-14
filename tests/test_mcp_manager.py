"""Tests for src.mcp.manager — concurrent startup, status tracking."""

from __future__ import annotations

import threading
import time

from bareagent.mcp.config import MCPConfig, MCPServerConfig
from bareagent.mcp.errors import MCPHandshakeError
from bareagent.mcp.manager import MCPManager, ServerStatus
from bareagent.mcp.protocol import Response, decode_message
from bareagent.mcp.transport.base import Transport


class _ControllableTransport(Transport):
    """Transport that fakes handshake with a configurable delay before reply."""

    def __init__(
        self,
        *,
        reply_delay: float = 0.0,
        fail: bool = False,
    ) -> None:
        super().__init__()
        self._reply_delay = reply_delay
        self._fail = fail
        self._started = False
        self._closed = False

    def start(self) -> None:
        self._started = True

    def send(self, message: str) -> None:
        msg = decode_message(message)
        from bareagent.mcp.protocol import Request

        if not isinstance(msg, Request):
            return

        def _reply() -> None:
            if self._reply_delay:
                time.sleep(self._reply_delay)
            if self._closed:
                return
            response = Response(
                id=msg.id,
                result={
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "1.0"},
                },
            )
            self._route_response(response)

        if msg.method == "initialize" and self._fail:
            self._route_response(
                Response(
                    id=msg.id,
                    result=None,
                    error=__import__(
                        "bareagent.mcp.protocol", fromlist=["ErrorObject"]
                    ).ErrorObject(code=-32603, message="nope"),
                )
            )
            return

        if msg.method == "initialize":
            threading.Thread(target=_reply, daemon=True).start()

    def close(self) -> None:
        self._closed = True
        self._fail_all_pending("controllable transport closed")

    def is_alive(self) -> bool:
        return self._started and not self._closed


def _patch_construct_transport(
    manager: MCPManager, transports: dict[str, Transport]
) -> None:
    def _factory(server: MCPServerConfig) -> Transport:
        return transports[server.name]

    manager._construct_transport = _factory  # type: ignore[assignment]


def test_start_all_parallel_with_one_slow_server_marks_it_unhealthy() -> None:
    fast_cfg = MCPServerConfig(
        name="fast", transport="stdio", command=["echo"], start_timeout=2.0
    )
    slow_cfg = MCPServerConfig(
        name="slow", transport="stdio", command=["echo"], start_timeout=0.2
    )
    transports = {
        "fast": _ControllableTransport(reply_delay=0.0),
        "slow": _ControllableTransport(reply_delay=1.0),  # > timeout
    }
    manager = MCPManager(MCPConfig(servers=[fast_cfg, slow_cfg]))
    _patch_construct_transport(manager, transports)

    start = time.monotonic()
    manager.start_all()
    elapsed = time.monotonic() - start

    # Boot was parallel: the slow timeout (0.2) caps the runtime, not blocking.
    assert elapsed < 2.0

    assert manager.get_status("fast") == ServerStatus.RUNNING
    assert manager.get_status("slow") == ServerStatus.UNHEALTHY

    fast_client = manager.get_client("fast")
    slow_client = manager.get_client("slow")
    assert fast_client is not None
    assert slow_client is None  # unhealthy → no client

    manager.close_all()


def test_get_client_returns_none_for_unhealthy_or_missing() -> None:
    fail_cfg = MCPServerConfig(name="fail", transport="stdio", command=["x"])
    manager = MCPManager(MCPConfig(servers=[fail_cfg]))
    _patch_construct_transport(manager, {"fail": _ControllableTransport(fail=True)})

    manager.start_all()

    assert manager.get_status("fail") == ServerStatus.UNHEALTHY
    assert manager.get_client("fail") is None
    assert manager.get_client("never-configured") is None

    manager.close_all()


def test_iter_running_clients_only_yields_running_servers() -> None:
    ok = MCPServerConfig(name="ok", transport="stdio", command=["echo"])
    bad = MCPServerConfig(name="bad", transport="stdio", command=["echo"])
    transports = {
        "ok": _ControllableTransport(),
        "bad": _ControllableTransport(fail=True),
    }
    manager = MCPManager(MCPConfig(servers=[ok, bad]))
    _patch_construct_transport(manager, transports)
    manager.start_all()

    running = list(manager.iter_running_clients())
    assert [name for name, _ in running] == ["ok"]
    manager.close_all()


def test_close_all_marks_all_servers_stopped() -> None:
    ok = MCPServerConfig(name="ok", transport="stdio", command=["echo"])
    manager = MCPManager(MCPConfig(servers=[ok]))
    _patch_construct_transport(manager, {"ok": _ControllableTransport()})
    manager.start_all()
    assert manager.get_status("ok") == ServerStatus.RUNNING

    manager.close_all()
    assert manager.get_status("ok") == ServerStatus.STOPPED


def test_summarize_returns_rows_in_config_order() -> None:
    a = MCPServerConfig(name="alpha", transport="stdio", command=["echo"])
    b = MCPServerConfig(name="beta", transport="stdio", command=["echo"])
    transports = {
        "alpha": _ControllableTransport(),
        "beta": _ControllableTransport(fail=True),
    }
    manager = MCPManager(MCPConfig(servers=[a, b]))
    _patch_construct_transport(manager, transports)
    manager.start_all()

    rows = manager.summarize()
    assert [row["name"] for row in rows] == ["alpha", "beta"]
    assert rows[0]["status"] == ServerStatus.RUNNING.value
    assert rows[1]["status"] == ServerStatus.UNHEALTHY.value
    # beta is unhealthy so its counts must be zero.
    assert rows[1]["tool_count"] == 0
    assert rows[1]["prompt_count"] == 0
    assert rows[1]["has_resources"] is False

    manager.close_all()


def test_reload_replaces_running_client_with_fresh_instance() -> None:
    cfg = MCPServerConfig(name="srv", transport="stdio", command=["echo"])
    first = _ControllableTransport()
    second = _ControllableTransport()
    transports = {"srv": first}
    manager = MCPManager(MCPConfig(servers=[cfg]))
    _patch_construct_transport(manager, transports)
    manager.start_all()
    assert manager.get_status("srv") == ServerStatus.RUNNING
    original_client = manager.get_client("srv")
    assert original_client is not None

    # Swap the transport so reload constructs a new client backed by it.
    transports["srv"] = second
    manager.reload("srv")

    assert manager.get_status("srv") == ServerStatus.RUNNING
    new_client = manager.get_client("srv")
    assert new_client is not None
    assert new_client is not original_client
    # Old transport was closed; new one is alive.
    assert first.is_alive() is False
    assert second.is_alive() is True

    manager.close_all()


def test_reload_marks_server_unhealthy_when_handshake_fails() -> None:
    cfg = MCPServerConfig(name="srv", transport="stdio", command=["echo"])
    ok_transport = _ControllableTransport()
    bad_transport = _ControllableTransport(fail=True)
    transports = {"srv": ok_transport}
    manager = MCPManager(MCPConfig(servers=[cfg]))
    _patch_construct_transport(manager, transports)
    manager.start_all()
    assert manager.get_status("srv") == ServerStatus.RUNNING

    transports["srv"] = bad_transport
    try:
        manager.reload("srv")
    except Exception:
        pass  # noqa: S110 — exception is expected; assert side-effects below
    else:
        raise AssertionError("reload should re-raise the handshake failure")

    assert manager.get_status("srv") == ServerStatus.UNHEALTHY
    assert manager.get_client("srv") is None

    manager.close_all()


def test_reload_unknown_server_raises_mcp_error() -> None:
    cfg = MCPServerConfig(name="known", transport="stdio", command=["echo"])
    manager = MCPManager(MCPConfig(servers=[cfg]))
    _patch_construct_transport(manager, {"known": _ControllableTransport()})
    manager.start_all()

    from bareagent.mcp.errors import MCPError

    raised = False
    try:
        manager.reload("missing")
    except MCPError as exc:
        raised = True
        assert "missing" in str(exc)
    assert raised, "reload of unknown server must raise MCPError"

    manager.close_all()


def test_on_disconnect_marks_unhealthy_and_notifies(monkeypatch) -> None:
    """PR6: an unexpected disconnect from the transport must flip status to
    UNHEALTHY, drop the client, and push a notification through the
    BackgroundManager-style notifier."""
    cfg = MCPServerConfig(name="srv", transport="stdio", command=["echo"])
    transport = _ControllableTransport()
    manager = MCPManager(MCPConfig(servers=[cfg]))
    _patch_construct_transport(manager, {"srv": transport})
    manager.start_all()
    assert manager.get_status("srv") == ServerStatus.RUNNING

    # Stand-in for ``BackgroundManager``: only ``notify`` needs to match the
    # real surface we use in production.
    notified: list[tuple[str, str]] = []

    class _FakeNotifier:
        def notify(self, task_id: str, message: str) -> None:
            notified.append((task_id, message))

    manager._notifier = _FakeNotifier()  # type: ignore[assignment]

    manager._on_disconnect("srv", "subprocess died: code 137")

    assert manager.get_status("srv") == ServerStatus.UNHEALTHY
    assert manager.get_client("srv") is None
    assert notified == [
        ("mcp:srv", "MCP server 'srv' disconnected: subprocess died: code 137")
    ]


def test_build_client_registers_disconnect_handler() -> None:
    """``_build_client`` must wire ``set_disconnect_handler`` on the transport
    so the manager learns about disconnects through the proactive path, not
    just on the next call."""
    cfg = MCPServerConfig(name="srv", transport="stdio", command=["echo"])
    transport = _ControllableTransport()
    manager = MCPManager(MCPConfig(servers=[cfg]))
    _patch_construct_transport(manager, {"srv": transport})
    manager.start_all()
    assert manager.get_status("srv") == ServerStatus.RUNNING

    # The handler is registered as a closure; trip it directly to confirm it
    # routes back to ``_on_disconnect`` rather than to nowhere.
    assert transport._disconnect_handler is not None  # noqa: SLF001
    transport._disconnect_handler("simulated")  # type: ignore[misc]  # noqa: SLF001
    assert manager.get_status("srv") == ServerStatus.UNHEALTHY


def test_summarize_reflects_unhealthy_after_disconnect() -> None:
    """After ``_on_disconnect`` the per-server summary row drops to UNHEALTHY
    with zeroed counts — that is what ``/mcp status`` renders to the REPL."""
    cfg = MCPServerConfig(name="srv", transport="stdio", command=["echo"])
    manager = MCPManager(MCPConfig(servers=[cfg]))
    _patch_construct_transport(manager, {"srv": _ControllableTransport()})
    manager.start_all()
    rows_before = manager.summarize()
    assert rows_before[0]["status"] == ServerStatus.RUNNING.value

    manager._on_disconnect("srv", "EOF")
    rows_after = manager.summarize()
    assert rows_after[0]["status"] == ServerStatus.UNHEALTHY.value
    assert rows_after[0]["tool_count"] == 0
    assert rows_after[0]["prompt_count"] == 0
    assert rows_after[0]["has_resources"] is False


def test_handshake_handshake_error_is_caught_and_warned(monkeypatch) -> None:
    """Even MCPHandshakeError surfaced from client.start is caught."""
    cfg = MCPServerConfig(name="boom", transport="stdio", command=["echo"])
    manager = MCPManager(MCPConfig(servers=[cfg]))

    def _bad_transport(server: MCPServerConfig) -> Transport:  # noqa: ARG001
        class _Broken(Transport):
            def start(self) -> None:
                raise MCPHandshakeError("transport refused")

            def send(self, message: str) -> None: ...
            def close(self) -> None: ...
            def is_alive(self) -> bool:
                return False

        return _Broken()

    manager._construct_transport = _bad_transport  # type: ignore[assignment]
    manager.start_all()
    assert manager.get_status("boom") == ServerStatus.UNHEALTHY
