"""Diagnostic snapshot + diff helpers for the Hybrid auto-diagnostics hook.

Surfaces three small primitives:

* :func:`snapshot_diagnostics` — read whatever diagnostics the LSP manager has
  for ``file_path`` right now, as a normalized list of :class:`Diagnostic`.
* :func:`diff_diagnostics` — given two snapshots (before / after), return the
  rows that appeared in ``after`` but not in ``before``. Equivalence is the
  five-tuple ``(file, line, col, severity, message)`` (see :class:`DiagnosticKey`).
* :func:`maybe_diagnostics_appendix` — handler-side entry point that wires the
  pieces together and answers "should I append a diagnostics paragraph to my
  tool result?". Returns ``None`` whenever LSP is unavailable, the config flag
  is off, or there were no newly-introduced diagnostics — i.e. the **happy
  path returns None** so the cost of feature-disabled callers is ~zero.

multilspy 0.0.15 explicitly registers ``do_nothing`` for
``textDocument/publishDiagnostics`` on every bundled language-server adapter
(see ``multilspy/language_servers/*/<server>.py``). There is no public pull-
diagnostics surface on ``SyncLanguageServer`` either. To bridge the gap, the
manager hooks the underlying ``LanguageServerHandler.on_notification_handlers``
post-handshake and routes publishDiagnostics into a per-server cache; this
module just reads from there via ``manager.get_diagnostics_snapshot(...)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import LSPConfig
    from .manager import LanguageServerManager


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """Normalized view of one LSP diagnostic.

    ``line`` / ``col`` are **1-based** (matching the rest of the LSP tool
    surface). ``severity`` is the editor-friendly label produced by
    :func:`_severity_label` (e.g. ``"Error"``).
    """

    file: str
    line: int
    col: int
    severity: str
    message: str
    source: str = ""


@dataclass(frozen=True, slots=True)
class DiagnosticKey:
    """Five-tuple identity for a diagnostic — see PRD diff algorithm.

    Two ``Diagnostic`` values that produce the same ``DiagnosticKey`` are
    treated as the same diagnostic for diff purposes. ``source`` is excluded
    deliberately: pyright sometimes leaves it blank, so including it would
    cause spurious "newly introduced" hits on otherwise identical rows.
    """

    file: str
    line: int
    col: int
    severity: str
    message: str

    @classmethod
    def from_diag(cls, diag: Diagnostic) -> DiagnosticKey:
        return cls(
            file=diag.file,
            line=diag.line,
            col=diag.col,
            severity=diag.severity,
            message=diag.message,
        )


_SEVERITY_LABELS = {1: "Error", 2: "Warning", 3: "Info", 4: "Hint"}


def _severity_label(severity: Any) -> str:
    try:
        return _SEVERITY_LABELS.get(int(severity), "Diagnostic")
    except (TypeError, ValueError):
        return "Diagnostic"


def _normalize(file_path: str, raw: Any) -> Diagnostic | None:
    """Convert a raw multilspy / LSP payload dict into a :class:`Diagnostic`.

    Returns ``None`` when the row is too malformed to be useful (e.g. no
    ``range``). The handlers never trust LSP server output blindly — pyright
    has been known to send notifications with empty arrays, and our diff
    algorithm requires a numeric position to compare against.
    """
    if not isinstance(raw, dict):
        return None
    range_ = raw.get("range") or {}
    start = range_.get("start") if isinstance(range_, dict) else None
    if not isinstance(start, dict):
        return None
    try:
        line0 = int(start.get("line", 0) or 0)
        col0 = int(start.get("character", 0) or 0)
    except (TypeError, ValueError):
        return None
    severity = _severity_label(raw.get("severity"))
    message = str(raw.get("message", ""))
    source = str(raw.get("source", "") or "")
    return Diagnostic(
        file=file_path,
        line=line0 + 1,  # 0-based LSP → 1-based for display + diff
        col=col0 + 1,
        severity=severity,
        message=message,
        source=source,
    )


def snapshot_diagnostics(
    manager: LanguageServerManager,
    file_path: str,
) -> list[Diagnostic]:
    """Return the manager's cached diagnostics for ``file_path``.

    Reads through :meth:`LanguageServerManager.get_diagnostics_snapshot`, which
    drains whatever ``publishDiagnostics`` notifications have arrived from the
    server so far. Returns an empty list when:

    * the file does not route to any configured server,
    * the routed server is not RUNNING,
    * the server hasn't published any diagnostics for the file yet.
    """
    raw_rows = manager.get_diagnostics_snapshot(file_path)
    out: list[Diagnostic] = []
    for raw in raw_rows:
        diag = _normalize(file_path, raw)
        if diag is not None:
            out.append(diag)
    return out


def diff_diagnostics(
    before: list[Diagnostic],
    after: list[Diagnostic],
) -> list[Diagnostic]:
    """Return rows in ``after`` whose five-tuple key is absent from ``before``.

    Order follows ``after`` so the appendix reads top-to-bottom in source
    order. The function is pure — both inputs may be reused.
    """
    before_keys = {DiagnosticKey.from_diag(d) for d in before}
    return [d for d in after if DiagnosticKey.from_diag(d) not in before_keys]


def format_diagnostics(file_path: str, diags: list[Diagnostic]) -> str:
    """Render newly-introduced diagnostics as a stable text block.

    The format is fixed by the PRD so downstream tooling (and the LLM) can
    detect the appendix by prefix::

        Newly introduced diagnostics in <file>:
        - [pyright Error] Line 12:5 — Cannot assign to variable 'x' because of its type

    ``source`` defaults to ``"lsp"`` when the server didn't include one. The
    leading file header is always emitted even if ``diags`` is empty so callers
    that pre-filter still produce a useful message; in practice the caller
    (``maybe_diagnostics_appendix``) skips the empty case entirely.
    """
    header = f"Newly introduced diagnostics in {file_path}:"
    if not diags:
        return header
    lines = [header]
    for diag in diags:
        source = diag.source or "lsp"
        lines.append(
            f"- [{source} {diag.severity}] Line {diag.line}:{diag.col} — {diag.message}"
        )
    return "\n".join(lines)


def maybe_diagnostics_appendix(
    manager: LanguageServerManager | None,
    lsp_config: LSPConfig | None,
    file_path: str,
    before: list[Diagnostic] | None,
) -> str | None:
    """Best-effort hook for edit/write handlers.

    Returns the formatted appendix (with leading ``\\n\\n`` so the caller can
    just ``result + appendix``) when **all** of the following are true:

    * ``manager`` and ``lsp_config`` are both provided,
    * ``lsp_config.auto_diagnostics_on_edit`` is True,
    * the file routes to a RUNNING server,
    * the after-snapshot has rows that were absent in ``before``.

    Returns ``None`` otherwise. The config gate is the first check so callers
    that disabled the feature pay only an attribute access (≪ 1µs). Any
    unexpected exception is swallowed — the handler must keep working even if
    the LSP subsystem is misbehaving.
    """
    if manager is None or lsp_config is None:
        return None
    if not lsp_config.auto_diagnostics_on_edit:
        return None
    if manager.language_for_file(file_path) is None:
        return None
    # ``get_server_for_file`` returns None when the server isn't RUNNING,
    # which we treat the same as "no diagnostics to compare" — drop out
    # silently rather than spamming an Error line on every edit.
    if manager.get_server_for_file(file_path) is None:
        return None

    try:
        # Briefly wait for pyright/etc. to publish the latest analysis pass.
        # The manager exposes the per-file Event; a 1.5s budget is enough for
        # incremental analysis on a medium repo and short enough to avoid
        # noticeable handler latency. Race condition mitigation modeled after
        # Serena's analysis_complete Event (see PRD Technical Approach).
        manager.wait_for_diagnostics(file_path, timeout=1.5)
        after = snapshot_diagnostics(manager, file_path)
    except Exception:
        return None

    new_diags = diff_diagnostics(before or [], after)
    if not new_diags:
        return None
    return "\n\n" + format_diagnostics(file_path, new_diags)


__all__ = [
    "Diagnostic",
    "DiagnosticKey",
    "diff_diagnostics",
    "format_diagnostics",
    "maybe_diagnostics_appendix",
    "snapshot_diagnostics",
]
