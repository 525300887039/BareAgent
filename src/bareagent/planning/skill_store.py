"""Storage for experientially generated skills (drafts + promotion).

Generated skills live OUTSIDE the repo's checked-in ``skills/`` (which is the
hand-written canon) to avoid polluting version control. They go under the
user-global BareAgent home, project-isolated by workspace slug — mirroring the
persistent-memory directory convention (``derive_memory_slug``). Drafts land in
a ``.pending/`` subdirectory and only become loadable once the user promotes
them with ``/skill keep``.

Layout (``root`` = generated skills root for this project)::

    <root>/<skill>/SKILL.md            # live (loadable) generated skills
    <root>/.pending/<skill>/SKILL.md   # drafts awaiting /skill keep

Pure filesystem logic with no LLM/loop dependency — unit-testable in isolation.
The agent-facing ``skill_create`` tool and the ``/skill`` REPL command are thin
layers over this store.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from bareagent.core.fileutil import atomic_write_text
from bareagent.memory.persistent import derive_memory_slug

PENDING_DIRNAME = ".pending"
_SKILL_FILE = "SKILL.md"


class SkillStoreError(ValueError):
    """Raised for caller-facing storage failures (bad name, missing draft)."""


def derive_skill_slug(name: str) -> str:
    """Slugify a skill name into a safe single path segment.

    Lowercases, collapses any non ``[a-z0-9]`` run to a single hyphen, and
    strips leading/trailing hyphens. This also neutralizes path traversal
    (``../`` etc.) since separators become hyphens. Empty result is rejected by
    the callers as an invalid name.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug


def default_generated_skills_root(workspace: Path) -> Path:
    """Per-project generated-skills directory under the user-global home."""
    return Path.home() / ".bareagent" / "projects" / derive_memory_slug(workspace) / "skills"


def resolve_generated_skills_root(workspace: Path, configured_dir: str) -> Path:
    """Resolve the generated-skills root from config.

    Empty ``configured_dir`` falls back to :func:`default_generated_skills_root`.
    A relative override is taken relative to the workspace; an absolute one is
    used as-is. Mirrors ``resolve_memory_root``.
    """
    configured = (configured_dir or "").strip()
    if not configured:
        return default_generated_skills_root(workspace)
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    return candidate


def _render_skill_md(name: str, description: str, body: str) -> str:
    """Render SKILL.md in the format ``SkillLoader`` expects.

    The description must be the first non-empty, non-``#`` line so
    ``SkillLoader._extract_description`` picks it up.
    """
    desc = description.strip() or "No description provided."
    sections = [f"# {name}", "", desc]
    body = body.strip()
    if body:
        sections += ["", body]
    return "\n".join(sections).rstrip() + "\n"


@dataclass(slots=True)
class SkillStore:
    """Create / promote / discard / list generated skills under ``root``."""

    root: Path

    @property
    def pending_root(self) -> Path:
        return self.root / PENDING_DIRNAME

    def create_draft(self, name: str, description: str, body: str) -> str:
        """Write a draft SKILL.md under ``.pending/<slug>/`` and return a note."""
        slug = derive_skill_slug(name)
        if not slug:
            raise SkillStoreError(f"invalid skill name: {name!r}")
        target = self.pending_root / slug / _SKILL_FILE
        atomic_write_text(target, _render_skill_md(slug, description, body))
        return f"Drafted skill '{slug}' to pending (use /skill keep {slug} to keep it)."

    def promote(self, name: str) -> str:
        """Move a draft from ``.pending/`` to the live root (replacing any live)."""
        slug = derive_skill_slug(name)
        src = self.pending_root / slug
        if not (src / _SKILL_FILE).exists():
            raise SkillStoreError(f"no pending draft named {slug!r}")
        dest = self.root / slug
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return f"Promoted skill '{slug}' — it is now loadable."

    def discard(self, name: str) -> str:
        """Delete a pending draft."""
        slug = derive_skill_slug(name)
        src = self.pending_root / slug
        if not (src / _SKILL_FILE).exists():
            raise SkillStoreError(f"no pending draft named {slug!r}")
        shutil.rmtree(src, ignore_errors=True)
        return f"Discarded pending skill '{slug}'."

    def list_live(self) -> list[str]:
        """Names of promoted (loadable) generated skills, sorted."""
        return self._list_skill_dirs(self.root)

    def list_pending(self) -> list[str]:
        """Names of pending drafts, sorted."""
        return self._list_skill_dirs(self.pending_root)

    def prune_pending(self, max_pending: int) -> list[str]:
        """Drop oldest pending drafts beyond ``max_pending``; return removed names.

        Count-based soft cap (no time TTL): keep the ``max_pending`` newest
        drafts by SKILL.md mtime, remove the rest. ``max_pending <= 0`` disables
        pruning.
        """
        if max_pending <= 0:
            return []
        entries = self._pending_entries_by_mtime()
        if len(entries) <= max_pending:
            return []
        removed: list[str] = []
        for _mtime, slug, path in entries[: len(entries) - max_pending]:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(slug)
        return removed

    def _pending_entries_by_mtime(self) -> list[tuple[float, str, Path]]:
        """Pending ``(mtime, slug, dir)`` tuples, oldest first (ties by name)."""
        entries: list[tuple[float, str, Path]] = []
        try:
            paths = sorted(self.pending_root.glob(f"*/{_SKILL_FILE}"))
        except OSError:
            return []
        for skill_file in paths:
            try:
                mtime = skill_file.stat().st_mtime
            except OSError:
                mtime = 0.0
            entries.append((mtime, skill_file.parent.name, skill_file.parent))
        entries.sort(key=lambda item: (item[0], item[1]))
        return entries

    @staticmethod
    def _list_skill_dirs(base: Path) -> list[str]:
        try:
            return sorted(p.parent.name for p in base.glob(f"*/{_SKILL_FILE}"))
        except OSError:
            return []
