"""Resolution of the bundled config.toml and the user's local override.

config.toml ships *inside* the installed package (``src/bareagent/config.toml``)
as read-only defaults. The user's ``config.local.toml`` override, however, must
live somewhere writable:

* For the **bundled default** (read-only, inside the package / site-packages),
  the override lives in the **current working directory** — so an installed user
  can ``bareagent init`` / drop a ``config.local.toml`` next to where they run,
  and a developer running from the repo root keeps picking up the repo's
  ``config.local.toml``.
* For an **explicit** ``--config`` / ``BAREAGENT_CONFIG`` path, the override is
  the ``.local`` sibling of that file (unchanged historical behavior).

This module has no dependency on :mod:`bareagent.main`, so both ``main`` (load +
mtime watch) and :mod:`bareagent.provider.setup` (``init`` writer) can import it
without a circular import.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def bundled_config_path() -> Path:
    """Filesystem path to the config.toml bundled inside the package.

    importlib.resources resolves it for both editable installs (real source
    tree) and wheel installs (site-packages). BareAgent is never a zipapp, so the
    resource is always a real on-disk path safe to open later.
    """
    return Path(str(files("bareagent").joinpath("config.toml")))


DEFAULT_CONFIG_PATH = bundled_config_path()


def local_config_path(config_path: Path) -> Path:
    """Where the ``config.local.toml`` override lives for a given base config.

    Bundled default -> current working directory; explicit path -> its ``.local``
    sibling.
    """
    if config_path == DEFAULT_CONFIG_PATH:
        return Path.cwd() / "config.local.toml"
    return config_path.with_suffix("").with_name(
        config_path.stem + ".local" + config_path.suffix,
    )
