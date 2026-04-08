from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pytest

from src.debug.interaction_log import InteractionLogger
from src.debug.web_viewer import start_viewer


def _read_response(url: str):
    with urlopen(url, timeout=2) as response:
        return response.status, response.headers, response.read()


def _read_text(url: str) -> tuple[int, str, bytes]:
    status, headers, body = _read_response(url)
    return status, headers.get_content_type(), body


def _read_json(url: str) -> object:
    _, _, body = _read_text(url)
    return json.loads(body.decode("utf-8"))


def _wait_for_server(base_url: str) -> None:
    for _ in range(20):
        try:
            with urlopen(f"{base_url}/api/sessions", timeout=2):
                return
        except URLError:
            time.sleep(0.05)
    raise AssertionError("viewer server did not start in time")


@pytest.fixture
def viewer_server(
    tmp_path: Path,
) -> SimpleNamespace:
    log_dir = tmp_path / ".logs"
    logger = InteractionLogger(log_dir=log_dir, session_id="alpha")
    seq = logger.log_request(
        [
            {"role": "system", "content": "Follow the spec."},
            {"role": "user", "content": "Build the viewer."},
        ],
        [{"name": "echo", "parameters": {"type": "object"}}],
        provider_info={"model": "test-model"},
    )
    logger.log_response(
        seq,
        text="Done.",
        thinking="Rendered the page.",
        tool_calls=[{"id": "toolu_1", "name": "echo", "input": {"value": "hi"}}],
        input_tokens=12,
        output_tokens=8,
        duration_ms=34.5,
    )

    logger_other = InteractionLogger(log_dir=log_dir, session_id="beta")
    logger_other.log_request(
        [{"role": "user", "content": "Second session"}],
        [],
    )

    server, thread = start_viewer(logger, port=0, host="127.0.0.1")
    host, port = server.server_address[:2]
    base_url = f"http://{host}:{port}"
    _wait_for_server(base_url)

    try:
        yield SimpleNamespace(
            base_url=base_url,
            logger=logger,
            server=server,
            thread=thread,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_get_root_returns_html(viewer_server: SimpleNamespace) -> None:
    status, content_type, body = _read_text(f"{viewer_server.base_url}/")

    assert status == 200
    assert content_type == "text/html"
    html = body.decode("utf-8")
    assert "BareAgent Debug Viewer" in html
    assert "bareagent-debug-viewer-locale" in html
    assert 'data-locale="en"' in html
    assert 'data-locale="zh"' in html
    assert "中文" in html
    assert "切换语言" in html
    assert "navigator.languages" in html
    assert 'normalized.startsWith("en")' in html
    assert "Failed to load session timeline" in html


def test_get_sessions_returns_json_list(viewer_server: SimpleNamespace) -> None:
    status, headers, body = _read_response(f"{viewer_server.base_url}/api/sessions")
    payload = json.loads(body.decode("utf-8"))

    assert status == 200
    assert headers.get("Access-Control-Allow-Origin") is None
    assert payload == viewer_server.logger.list_sessions()


def test_get_session_interactions_returns_summary(viewer_server: SimpleNamespace) -> None:
    payload = _read_json(f"{viewer_server.base_url}/api/sessions/alpha")

    assert payload == viewer_server.logger.list_interactions("alpha")


def test_get_interaction_returns_full_data(viewer_server: SimpleNamespace) -> None:
    payload = _read_json(f"{viewer_server.base_url}/api/interactions/alpha/0")

    assert payload == viewer_server.logger.get_interaction("alpha", 0)


def test_get_nonexistent_returns_404(viewer_server: SimpleNamespace) -> None:
    with pytest.raises(HTTPError) as exc_info:
        urlopen(f"{viewer_server.base_url}/nonexistent", timeout=2)

    assert exc_info.value.code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions/..%5Csecret",
        "/api/interactions/..%5Csecret/0",
    ],
)
def test_invalid_session_paths_return_404(
    viewer_server: SimpleNamespace,
    path: str,
) -> None:
    with pytest.raises(HTTPError) as exc_info:
        urlopen(f"{viewer_server.base_url}{path}", timeout=2)

    assert exc_info.value.code == 404
