from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Iterator

IGNORED_PATH_NAMES = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


def is_ignored_descendant(path: Path, search_root: Path) -> bool:
    """Skip ignored trees unless the caller explicitly searched inside them."""
    relative = path.resolve(strict=False).relative_to(search_root.resolve(strict=False))
    return any(part in IGNORED_PATH_NAMES for part in relative.parts)


def iter_search_files(search_root: Path) -> Iterator[Path]:
    if search_root.is_file():
        yield search_root
        return

    resolved_root = search_root.resolve(strict=False)
    for current_root, dir_names, file_names in os.walk(resolved_root):
        current_path = Path(current_root)
        dir_names[:] = sorted(
            name
            for name in dir_names
            if not is_ignored_descendant(current_path / name, resolved_root)
        )
        for file_name in sorted(file_names):
            file_path = current_path / file_name
            if is_ignored_descendant(file_path, resolved_root):
                continue
            yield file_path


def matches_glob_pattern(candidate: Path, search_root: Path, pattern: str) -> bool:
    pattern_norm = pattern.replace("\\", "/")
    relative = candidate.resolve(strict=False).relative_to(search_root.resolve(strict=False))
    relative_posix = relative.as_posix()

    if "/" in pattern_norm or "**" in pattern_norm:
        return any(
            PurePosixPath(relative_posix).match(variant)
            for variant in _expand_recursive_variants(pattern_norm)
        )
    return PurePosixPath(candidate.name).match(pattern_norm)


def requires_recursive_walk(pattern: str) -> bool:
    pattern_norm = pattern.replace("\\", "/")
    return "/" in pattern_norm or "**" in pattern_norm


def _expand_recursive_variants(pattern: str) -> set[str]:
    variants = {pattern}
    changed = True
    while changed:
        changed = False
        new_variants: set[str] = set()
        for variant in variants:
            index = variant.find("**/")
            while index != -1:
                new_variants.add(variant[:index] + variant[index + 3 :])
                index = variant.find("**/", index + 1)
        extra = new_variants - variants
        if extra:
            variants.update(extra)
            changed = True
    return variants
