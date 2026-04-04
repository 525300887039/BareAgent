"""Shared file-system and small utilities."""

from __future__ import annotations

import json
import os
import secrets
import string
import tempfile
from pathlib import Path
from typing import Any

_ID_ALPHABET = string.ascii_letters + string.digits


def generate_random_id(length: int = 8) -> str:
    """Return a cryptographically random alphanumeric string."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


def atomic_write_json(file_path: Path, payload: Any) -> None:
    """Atomically write *payload* as JSON to *file_path* via tempfile + rename."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(file_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(file_path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
