from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from bareagent.core.sandbox import safe_path

# Image extension -> Anthropic-supported mime type. The whitelist mirrors
# ``mcp/registry.py:_SUPPORTED_IMAGE_MIME_TYPES`` so local image reads produce
# the same internal block shape MCP multimodal results do.
_IMAGE_EXT_TO_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Hard cap on image payloads. Aligned with the MCP default
# ``max_result_binary_bytes`` (5 MiB). Over-limit images return an Error string
# (D2: ask the user to downscale; we do not auto-resize — that needs Pillow).
_MAX_IMAGE_BYTES = 5_242_880  # 5 MiB

# Raw-file cap for native PDF document blocks. ponytail: derived from Anthropic's
# 32 MB per-request limit and base64's ~1.33x inflation, with headroom for the
# rest of the request; bump here if Anthropic raises the request cap.
_MAX_PDF_BYTES = 20 * 1024 * 1024  # 20 MiB

# Per-output and whole-document length caps for textual renderings (notebook
# cell outputs, PDF text). Keeps a pathological file from flooding the context.
_MAX_CELL_OUTPUT_CHARS = 2000
_MAX_DOCUMENT_CHARS = 200_000

_TRUNCATED_MARKER = "... [truncated]"

# Returned when the image path is reached but the active model has no vision
# capability (see provider/capabilities.py). We refuse to emit the image block
# rather than let it hit the provider API and error out.
_IMAGE_DISABLED_ERROR = (
    "Error: the current model has no image (vision) capability, so this image was "
    "not sent to avoid an API error. Switch to a vision-capable model, or set "
    "[capabilities] image_in = true (or env BAREAGENT_MODEL_IMAGE_IN=1) if this "
    "model does support images."
)


def run_read(
    file_path: str,
    offset: int = 0,
    limit: int | None = None,
    *,
    pages: str | None = None,
    workspace: Path,
    image_enabled: bool = True,
    pdf_enabled: bool = False,
) -> str | list[dict[str, Any]]:
    """Read a workspace file, dispatching by extension.

    - Images (.png/.jpg/.jpeg/.gif/.webp) -> ``[text, image]`` content blocks
      when ``image_enabled`` (the model has vision); otherwise a friendly Error.
    - PDF (.pdf) -> a native ``document`` block when ``pdf_enabled`` (the model
      supports native PDF), ``pages`` is not given, and the file is under the
      size cap; otherwise extracted text via the ``[pdf]`` extra (pypdf).
    - Notebook (.ipynb) -> text rendering of markdown/code cells + outputs.
    - Everything else -> the legacy UTF-8 text path with line numbers
      (``offset`` / ``limit`` slice).
    """
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")

    resolved = safe_path(file_path, workspace)
    suffix = resolved.suffix.lower()

    if suffix in _IMAGE_EXT_TO_MIME:
        if not image_enabled:
            return _IMAGE_DISABLED_ERROR
        return _read_image(resolved, _IMAGE_EXT_TO_MIME[suffix])
    if suffix == ".pdf":
        return _read_pdf_document(resolved, pages, pdf_enabled)
    if suffix == ".ipynb":
        return _read_notebook(resolved)

    return _read_text(resolved, offset, limit)


def _read_text(resolved: Path, offset: int, limit: int | None) -> str:
    """Read a UTF-8 text file and prefix each line with its line number."""
    if offset > 0 or limit is not None:
        # Stream lines to avoid loading the entire file when only a slice is needed.
        selected: list[str] = []
        end = None if limit is None else offset + limit
        with resolved.open(encoding="utf-8") as fh:
            for idx, line in enumerate(fh):
                if idx < offset:
                    continue
                if end is not None and idx >= end:
                    break
                selected.append(line.rstrip("\n\r"))
    else:
        selected = resolved.read_text(encoding="utf-8").splitlines()

    return "\n".join(
        f"{line_number}: {line}" for line_number, line in enumerate(selected, start=offset + 1)
    )


def _read_image(resolved: Path, mime: str) -> str | list[dict[str, Any]]:
    """Read an image as base64 and return ``[text, image]`` content blocks."""
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        return f"Error: cannot read image {resolved.name!r}: {exc}"
    size = len(raw)
    if size > _MAX_IMAGE_BYTES:
        return (
            f"Error: image {resolved.name!r} is {size} bytes, exceeding the "
            f"{_MAX_IMAGE_BYTES} byte limit. Downscale or compress it before reading."
        )
    return build_image_blocks(raw, mime, resolved.name)


def build_image_blocks(raw: bytes, mime: str, label: str) -> list[dict[str, Any]]:
    """Build ``[text, image]`` content blocks from raw image bytes.

    Shared by the local file path (``_read_image``) and web_fetch, so the block
    shape and base64 encoding stay single-source. The caller enforces the size
    cap (``_MAX_IMAGE_BYTES``) — this helper just encodes.
    """
    data = base64.b64encode(raw).decode("ascii")
    return [
        {"type": "text", "text": f"Image {label} ({mime}, {len(raw)} bytes)"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": data,
            },
        },
    ]


def _read_notebook(resolved: Path) -> str:
    """Render a Jupyter notebook's markdown/code cells (+ outputs) as text."""
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Error: cannot read notebook {resolved.name!r}: {exc}"
    try:
        nb = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return f"Error: notebook {resolved.name!r} is not valid JSON: {exc}"

    cells = nb.get("cells") if isinstance(nb, dict) else None
    if not isinstance(cells, list):
        return f"Error: notebook {resolved.name!r} has no 'cells' array"

    parts: list[str] = []
    for index, cell in enumerate(cells, start=1):
        if not isinstance(cell, dict):
            continue
        cell_type = cell.get("cell_type")
        source = _join_source(cell.get("source"))
        if cell_type == "markdown":
            parts.append(f"## Markdown cell {index}\n{source}")
        elif cell_type == "code":
            block = f"## Code cell {index}\n{source}"
            outputs = _render_outputs(cell.get("outputs"))
            if outputs:
                block += f"\n### Output\n{outputs}"
            parts.append(block)
        # Other cell types (e.g. raw) are skipped.

    rendered = "\n\n".join(parts)
    return _cap_document(rendered)


def _join_source(source: Any) -> str:
    """Notebook ``source`` is a list of lines or a single string."""
    if isinstance(source, list):
        return "".join(str(item) for item in source)
    if isinstance(source, str):
        return source
    return ""


def _render_outputs(outputs: Any) -> str:
    """Extract human-readable text from a code cell's ``outputs`` array."""
    if not isinstance(outputs, list):
        return ""
    rendered: list[str] = []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        output_type = output.get("output_type")
        text = ""
        if output_type == "stream":
            text = _join_source(output.get("text"))
        elif output_type in ("execute_result", "display_data"):
            data = output.get("data")
            if isinstance(data, dict):
                text = _join_source(data.get("text/plain"))
        elif output_type == "error":
            traceback = output.get("traceback")
            if isinstance(traceback, list):
                text = "\n".join(str(line) for line in traceback)
        if text:
            rendered.append(_truncate(text, _MAX_CELL_OUTPUT_CHARS))
    return "\n".join(rendered)


def _read_pdf_document(
    resolved: Path,
    pages: str | None,
    pdf_enabled: bool,
) -> str | list[dict[str, Any]]:
    """Dispatch a PDF read between the native document block and pypdf text.

    - Explicit ``pages`` -> pypdf text (native blocks can't select pages).
    - ``pdf_enabled`` + under the size cap -> native ``document`` block.
    - No capability / over the cap -> pypdf text (over-cap adds a one-line note).
    """
    if pages is not None:
        return _read_pdf(resolved, pages)
    if not pdf_enabled:
        return _read_pdf(resolved, None)
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        return f"Error: cannot read PDF {resolved.name!r}: {exc}"
    if size > _MAX_PDF_BYTES:
        note = (
            f"Note: PDF {resolved.name!r} is {size} bytes, over the {_MAX_PDF_BYTES} "
            "byte native limit; extracted its text instead.\n\n"
        )
        text = _read_pdf(resolved, None)
        return note + text if isinstance(text, str) else text
    return _read_pdf_native(resolved)


def _read_pdf_native(resolved: Path) -> str | list[dict[str, Any]]:
    """Read a PDF as a base64 ``document`` block (Anthropic native PDF)."""
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        return f"Error: cannot read PDF {resolved.name!r}: {exc}"
    data = base64.b64encode(raw).decode("ascii")
    return [
        {"type": "text", "text": f"PDF {resolved.name} ({len(raw)} bytes, native document)"},
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": data,
            },
        },
    ]


def _read_pdf(resolved: Path, pages: str | None) -> str:
    """Extract text from a PDF. Needs the ``[pdf]`` extra (pypdf)."""
    try:
        from pypdf import PdfReader  # type: ignore[import-untyped]
    except ImportError:
        return (
            'Error: PDF support is not installed. Run uv pip install -e ".[pdf]" '
            "(or pip install pypdf)."
        )

    try:
        reader = PdfReader(str(resolved))
    except Exception as exc:  # noqa: BLE001 - pypdf raises a variety of types
        return f"Error: cannot open PDF {resolved.name!r}: {type(exc).__name__}: {exc}"

    total = len(reader.pages)
    if total == 0:
        return f"Error: PDF {resolved.name!r} has no pages"

    selection = _parse_page_range(pages, total)
    if isinstance(selection, str):
        return selection  # error message

    parts: list[str] = []
    for page_no in selection:
        try:
            text = reader.pages[page_no].extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - extraction can fail per page
            text = f"[Error extracting text: {type(exc).__name__}: {exc}]"
        parts.append(f"--- Page {page_no + 1} ---\n{text}")

    return _cap_document("\n\n".join(parts))


def _parse_page_range(pages: str | None, total: int) -> list[int] | str:
    """Parse a 1-based user page spec into a list of 0-based indices.

    Accepts ``None`` (all pages), ``"3"`` (single page) and ``"1-5"`` (range).
    A range's ``end`` past the last page is clamped, but a ``start`` past the
    last page is an explicit error (mirroring the single-page branch) rather
    than silently returning a different page. Returns an Error string on bad
    input.
    """
    if pages is None:
        return list(range(total))
    spec = pages.strip()
    if not spec:
        return list(range(total))

    if "-" in spec:
        start_str, _, end_str = spec.partition("-")
        try:
            start = int(start_str)
            end = int(end_str)
        except ValueError:
            return f"Error: invalid page range {pages!r} (expected e.g. '1-5' or '3')"
        if start < 1 or end < 1 or start > end:
            return f"Error: invalid page range {pages!r}"
        if start > total:
            return f"Error: page range start {start} out of range (PDF has {total} pages)"
        start_idx = start - 1
        end_idx = min(end, total)
        return list(range(start_idx, end_idx))

    try:
        single = int(spec)
    except ValueError:
        return f"Error: invalid page range {pages!r} (expected e.g. '1-5' or '3')"
    if single < 1:
        return f"Error: invalid page number {pages!r}"
    if single > total:
        return f"Error: page {single} out of range (PDF has {total} pages)"
    return [single - 1]


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATED_MARKER


def _cap_document(text: str) -> str:
    if len(text) <= _MAX_DOCUMENT_CHARS:
        return text
    return text[:_MAX_DOCUMENT_CHARS] + f"\n{_TRUNCATED_MARKER}"
