"""Session fork / tree branching (REPL-independent, unit-testable).

Pure helpers backing the ``/fork`` and ``/tree`` REPL commands. No dependency on
``bareagent.main`` / UI / loop, so this module can be exercised in isolation.

Three concerns:

- **Fork-point enumeration + legal slicing** (:func:`enumerate_fork_points`,
  :func:`slice_for_fork_point`) — the correctness core. A fork keeps a deep copy
  of ``messages[0:cut]``; ``cut`` must land on a clean turn boundary so the slice
  keeps Anthropic role alternation, never splits a ``tool_use`` from its
  ``tool_result``, and can accept the next user turn. The only such boundary is
  *right after an assistant message that carries no ``tool_use`` block* — which is
  exactly where a turn ends. We only ever offer those, so there is no illegal
  pick to recover from.
- **Lineage sidecar** (:class:`ForkRecord`, :func:`load_tree`, :func:`record_fork`)
  — a ``child_session_id -> {parent, fork_point, parent_len, created}`` map stored
  in ``.transcripts/.tree.json``. Read is fail-open (missing / corrupt -> ``{}``);
  write is atomic (``atomic_write_json``) under a module lock.
- **Forest rendering** (:func:`render_tree`) — an ASCII tree over *all* sessions,
  with fork edges overlaid, the current node marked, and cycle protection.
"""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bareagent.core.fileutil import atomic_write_json

_PREVIEW_LIMIT = 60
_PREVIEW_PLACEHOLDER = "(no text)"
_TREE_FILENAME = ".tree.json"

# Serializes the read-modify-write of the lineage sidecar. Writes only ever come
# from the single REPL thread today, but a module lock keeps us aligned with the
# ``TaskManager`` persistence convention and is cheap.
_tree_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Fork points
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ForkPoint:
    """A legal place to branch the conversation.

    ``cut`` is the slice end: ``messages[0:cut]`` is the (pre-deep-copy) prefix
    the fork keeps. ``number`` is the 1-based index shown to the user.
    """

    number: int
    cut: int
    user_preview: str
    assistant_preview: str


def _has_tool_use(content: Any) -> bool:
    if isinstance(content, list):
        return any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content)
    return False


def _first_text(content: Any) -> str:
    """Pull the first text block out of a message's content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text", ""))
    return ""


def _preview(text: str, limit: int = _PREVIEW_LIMIT) -> str:
    collapsed = " ".join(text.split())
    if not collapsed:
        return _PREVIEW_PLACEHOLDER
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def _is_real_user_turn(message: dict[str, Any]) -> bool:
    """A user message that carries text (an actual prompt), not just tool_result."""
    return message.get("role") == "user" and bool(_first_text(message.get("content")))


def enumerate_fork_points(messages: list[dict[str, Any]]) -> list[ForkPoint]:
    """Enumerate every clean turn boundary in *messages*.

    A boundary exists right after each assistant message that carries no
    ``tool_use`` block (the end of a turn). Each point records the preview of the
    most recent real user prompt and of the assistant response, plus the ``cut``
    index for slicing. May be empty (e.g. only a system message, or the single
    turn so far is still mid-tool-cycle).
    """
    points: list[ForkPoint] = []
    last_user_preview = _PREVIEW_PLACEHOLDER
    number = 0
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "user":
            if _is_real_user_turn(message):
                last_user_preview = _preview(_first_text(message.get("content")))
            continue
        if role == "assistant" and not _has_tool_use(message.get("content")):
            number += 1
            points.append(
                ForkPoint(
                    number=number,
                    cut=index + 1,
                    user_preview=last_user_preview,
                    assistant_preview=_preview(_first_text(message.get("content"))),
                )
            )
    return points


def slice_for_fork_point(messages: list[dict[str, Any]], number: int) -> list[dict[str, Any]]:
    """Return a deep copy of the prefix for fork point *number*.

    The deep copy guarantees the new branch shares no mutable block dicts/lists
    with the parent conversation. Raises :class:`ValueError` if *number* does not
    match any enumerated point (out of range / no fork points available).
    """
    points = enumerate_fork_points(messages)
    for point in points:
        if point.number == number:
            return copy.deepcopy(messages[: point.cut])
    if not points:
        raise ValueError("no fork points available (need a completed assistant turn)")
    raise ValueError(f"fork point {number} out of range (1..{len(points)})")


# --------------------------------------------------------------------------- #
# Lineage sidecar
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ForkRecord:
    """One fork edge: *child* was branched from *parent* at a turn boundary.

    ``fork_point`` is the point number shown at fork time; ``parent_len`` is the
    message count of the kept prefix. Both are display-only (``forked from
    <parent> @ turn N``) and do not participate in reconstruction. ``created`` is
    an ISO-8601 timestamp supplied by the caller (this module stays clock-free so
    it remains deterministically testable).
    """

    parent: str
    fork_point: int
    parent_len: int
    created: str


def tree_path(transcript_dir: Path) -> Path:
    """Sidecar path inside the transcript directory.

    ``.tree.json`` is dot-prefixed so it is never matched by the ``*.jsonl`` glob
    that drives session listing.
    """
    return Path(transcript_dir) / _TREE_FILENAME


def _coerce_record(value: Any) -> ForkRecord | None:
    if not isinstance(value, dict):
        return None
    parent = value.get("parent")
    if not isinstance(parent, str) or not parent:
        return None
    try:
        fork_point = int(value.get("fork_point", 0))
        parent_len = int(value.get("parent_len", 0))
    except (TypeError, ValueError):
        return None
    created = value.get("created", "")
    return ForkRecord(
        parent=parent,
        fork_point=fork_point,
        parent_len=parent_len,
        created=str(created),
    )


def load_tree(path: Path) -> dict[str, ForkRecord]:
    """Load the lineage map, fail-open.

    Missing file, unreadable file, or non-object JSON yields ``{}``. Individual
    malformed entries are skipped so a single bad record never discards the rest.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(document, dict):
        return {}
    tree: dict[str, ForkRecord] = {}
    for child, value in document.items():
        if not isinstance(child, str) or not child:
            continue
        record = _coerce_record(value)
        if record is not None:
            tree[child] = record
    return tree


def record_fork(path: Path, child: str, record: ForkRecord) -> None:
    """Atomically add (or overwrite) the *child* lineage edge.

    Read-modify-write under a module lock, written via ``atomic_write_json``. The
    caller treats this as best-effort: a write failure must not abort the fork.
    """
    target = Path(path)
    with _tree_lock:
        tree = load_tree(target)
        tree[child] = record
        payload = {sid: asdict(rec) for sid, rec in tree.items()}
        atomic_write_json(target, payload)


# --------------------------------------------------------------------------- #
# Forest rendering
# --------------------------------------------------------------------------- #


def _build_children(
    sessions: list[str], tree: dict[str, ForkRecord]
) -> tuple[list[str], dict[str, list[str]]]:
    """Split *sessions* into roots and a parent -> children adjacency map.

    A session is a root when it has no fork record, or when its parent is not a
    known session (orphan -> shown as a root, fail-open). Child order follows the
    ``sessions`` order (newest first, as returned by ``list_sessions``).
    """
    known = set(sessions)
    children: dict[str, list[str]] = {sid: [] for sid in sessions}
    roots: list[str] = []
    for sid in sessions:
        record = tree.get(sid)
        if record is not None and record.parent in known:
            children[record.parent].append(sid)
        else:
            roots.append(sid)
    return roots, children


def render_tree(
    sessions: list[str],
    tree: dict[str, ForkRecord],
    current: str | None,
) -> str:
    """Render the session forest as an ASCII tree.

    Nodes are all *sessions*; edges come from *tree*; the *current* node is
    marked. Cycle-safe (a corrupt sidecar cannot cause infinite recursion).
    """
    if not sessions:
        return ""

    roots, children = _build_children(sessions, tree)
    lines: list[str] = []
    visited: set[str] = set()

    def annotate(sid: str) -> str:
        label = sid
        record = tree.get(sid)
        if record is not None and record.parent in children:
            label += f"  @ turn {record.fork_point}"
        if sid == current:
            label += "  ● current"
        return label

    def walk(sid: str, prefix: str, is_last: bool, is_root: bool) -> None:
        if is_root:
            connector = ""
            child_prefix = ""
        else:
            connector = "└─ " if is_last else "├─ "
            child_prefix = "   " if is_last else "│  "
        lines.append(f"{prefix}{connector}{annotate(sid)}")
        if sid in visited:
            return
        visited.add(sid)
        kids = children.get(sid, [])
        for position, child in enumerate(kids):
            walk(
                child,
                prefix + child_prefix,
                position == len(kids) - 1,
                is_root=False,
            )

    for sid in roots:
        walk(sid, "", True, is_root=True)

    return "\n".join(lines)
