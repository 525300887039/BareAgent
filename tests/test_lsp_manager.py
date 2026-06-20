"""Tests for ``src.lsp.manager`` — multi-language server manager.

All tests use :class:`FakeSyncLanguageServer` to avoid spawning a real
language server. The fake mimics the multilspy ``SyncLanguageServer`` public
surface used by the manager (``create`` + ``start_server`` context).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from bareagent.lsp import manager as manager_module
from bareagent.lsp.config import LSPConfig, LSPServerConfig
from bareagent.lsp.manager import LanguageServerManager, ServerStatus

# ---------------------------------------------------------------------------
# Fake multilspy.SyncLanguageServer
# ---------------------------------------------------------------------------


class FakeSyncLanguageServer:
    """Lightweight stand-in for ``multilspy.SyncLanguageServer``.

    The class methods accept the same public arguments as multilspy so the
    manager wiring stays exercised end-to-end without spawning a subprocess.
    """

    instances: list[FakeSyncLanguageServer] = []

    def __init__(
        self, language: str, *, start_delay: float = 0.0, raise_on_enter: bool = False
    ) -> None:
        self.language = language
        self.start_delay = start_delay
        self.raise_on_enter = raise_on_enter
        self.entered = False
        self.exited = False
        # Pre-canned responses; tests mutate these to assert handler behaviour.
        self.document_symbols_response: Any = ([], None)
        self.definition_response: list[dict[str, Any]] = []
        self.references_response: list[dict[str, Any]] = []
        self.last_definition_args: tuple[str, int, int] | None = None
        self.last_references_args: tuple[str, int, int] | None = None
        FakeSyncLanguageServer.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()

    @contextmanager
    def start_server(self):
        if self.start_delay:
            time.sleep(self.start_delay)
        if self.raise_on_enter:
            raise RuntimeError(f"boom: {self.language}")
        self.entered = True
        try:
            yield self
        finally:
            self.exited = True

    # multilspy request_* surface (subset used by the tools).
    def request_document_symbols(self, relative_file_path: str):
        return self.document_symbols_response

    def request_definition(self, relative_file_path: str, line: int, column: int):
        self.last_definition_args = (relative_file_path, line, column)
        return self.definition_response

    def request_references(self, relative_file_path: str, line: int, column: int):
        self.last_references_args = (relative_file_path, line, column)
        return self.references_response


def _make_fake_factory(*, start_delay: float = 0.0, raise_on_enter: bool = False):
    """Build a fake ``SyncLanguageServer`` class whose ``create`` returns a
    new :class:`FakeSyncLanguageServer` per language."""

    class _FakeCls:
        @classmethod
        def create(cls, config, logger, repository_root):
            language = (
                config.code_language
                if hasattr(config, "code_language")
                else config["code_language"]
            )
            return FakeSyncLanguageServer(
                str(language),
                start_delay=start_delay,
                raise_on_enter=raise_on_enter,
            )

    return _FakeCls


def _build_sync_server_factory(*, start_delay: float = 0.0, raise_on_enter: bool = False):
    def _build(sync_cls, server, repository_root):
        return sync_cls.create(
            SimpleNamespace(code_language=server.language),
            None,
            repository_root,
        )

    _build.start_delay = start_delay  # type: ignore[attr-defined]
    _build.raise_on_enter = raise_on_enter  # type: ignore[attr-defined]
    return _build


@pytest.fixture(autouse=True)
def _reset_fake_servers():
    FakeSyncLanguageServer.reset()
    yield
    FakeSyncLanguageServer.reset()


@pytest.fixture
def patch_multilspy(monkeypatch):
    """Patch the manager's lazy multilspy import + builder with the fake."""

    def _patch(*, start_delay: float = 0.0, raise_on_enter: bool = False):
        sync_cls = _make_fake_factory(start_delay=start_delay, raise_on_enter=raise_on_enter)
        monkeypatch.setattr(
            manager_module,
            "_import_sync_language_server",
            lambda: sync_cls,
        )
        monkeypatch.setattr(
            manager_module,
            "_build_sync_server",
            _build_sync_server_factory(start_delay=start_delay, raise_on_enter=raise_on_enter),
        )
        return sync_cls

    return _patch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_start_all_marks_all_running(patch_multilspy) -> None:
    patch_multilspy()
    cfg = LSPConfig(
        servers=[
            LSPServerConfig(language="python", extensions=[".py"]),
            LSPServerConfig(language="typescript", extensions=[".ts"]),
        ]
    )
    mgr = LanguageServerManager(cfg)
    mgr.start_all()
    assert mgr.get_status("python") == ServerStatus.RUNNING
    assert mgr.get_status("typescript") == ServerStatus.RUNNING
    running = dict(mgr.iter_running())
    assert set(running) == {"python", "typescript"}
    mgr.close_all()


def test_start_all_handles_handshake_failure(patch_multilspy) -> None:
    patch_multilspy(raise_on_enter=True)
    cfg = LSPConfig(servers=[LSPServerConfig(language="python", extensions=[".py"])])
    mgr = LanguageServerManager(cfg)
    mgr.start_all()
    assert mgr.get_status("python") == ServerStatus.UNHEALTHY
    assert "boom" in mgr.get_status_reason("python")
    assert dict(mgr.iter_running()) == {}


def test_start_all_handles_timeout(patch_multilspy) -> None:
    patch_multilspy(start_delay=0.5)
    cfg = LSPConfig(
        servers=[LSPServerConfig(language="python", extensions=[".py"])],
        start_timeout=0.05,
    )
    mgr = LanguageServerManager(cfg)
    mgr.start_all()
    assert mgr.get_status("python") == ServerStatus.UNHEALTHY
    assert "timed out" in mgr.get_status_reason("python")


def test_start_all_no_op_when_no_servers() -> None:
    mgr = LanguageServerManager(LSPConfig())
    mgr.start_all()
    assert list(mgr.iter_running()) == []


def test_multilspy_missing_marks_all_unhealthy(monkeypatch) -> None:
    monkeypatch.setattr(manager_module, "_import_sync_language_server", lambda: None)
    cfg = LSPConfig(
        servers=[
            LSPServerConfig(language="python", extensions=[".py"]),
            LSPServerConfig(language="rust", extensions=[".rs"]),
        ]
    )
    mgr = LanguageServerManager(cfg)
    mgr.start_all()
    assert mgr.get_status("python") == ServerStatus.UNHEALTHY
    assert mgr.get_status("rust") == ServerStatus.UNHEALTHY
    assert mgr.get_status_reason("python") == "multilspy extra not installed"


def test_get_server_for_file_routes_by_extension(patch_multilspy) -> None:
    patch_multilspy()
    cfg = LSPConfig(
        servers=[
            LSPServerConfig(language="python", extensions=[".py", ".pyi"]),
            LSPServerConfig(language="typescript", extensions=[".ts"]),
        ]
    )
    mgr = LanguageServerManager(cfg)
    mgr.start_all()
    py_server = mgr.get_server_for_file("/x/foo.py")
    assert py_server is not None
    assert py_server.language == "python"
    pyi_server = mgr.get_server_for_file("/x/foo.PYI")  # case-insensitive
    assert pyi_server is not None
    assert pyi_server.language == "python"
    ts_server = mgr.get_server_for_file("/x/foo.ts")
    assert ts_server is not None
    assert ts_server.language == "typescript"
    assert mgr.get_server_for_file("/x/foo.rs") is None
    mgr.close_all()


def test_get_server_for_file_returns_none_when_unhealthy(patch_multilspy) -> None:
    patch_multilspy(raise_on_enter=True)
    cfg = LSPConfig(servers=[LSPServerConfig(language="python", extensions=[".py"])])
    mgr = LanguageServerManager(cfg)
    mgr.start_all()
    assert mgr.get_server_for_file("/x/foo.py") is None


def test_close_all_marks_stopped(patch_multilspy) -> None:
    patch_multilspy()
    cfg = LSPConfig(servers=[LSPServerConfig(language="python", extensions=[".py"])])
    mgr = LanguageServerManager(cfg)
    mgr.start_all()
    mgr.close_all()
    assert mgr.get_status("python") == ServerStatus.STOPPED
    # iter_running should now be empty.
    assert list(mgr.iter_running()) == []
