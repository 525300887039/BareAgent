# Research: Hatchling src-layout + hatch-vcs + importlib.resources packaging

- **Query**: Best practice for a Hatchling src-layout package that (a) derives version from git tags via hatch-vcs and (b) bundles `config.toml` + `skills/` tree into the wheel/sdist and locates them at runtime via importlib.resources. Migrating `src/*` (flat, import pkg literally `src`) -> `src/bareagent/*`.
- **Scope**: external (hatch-vcs / hatchling / Python stdlib docs) + internal (current pyproject.toml + runtime resolution call sites)
- **Date**: 2026-06-14

## Current State (internal, what we are migrating FROM)

`D:\code\BareAgent\pyproject.toml`:
- `[build-system] requires = ["hatchling>=1.27"]`
- `version = "0.1.0"` hardcoded in `[project]`
- `[project.scripts] bareagent = "src.main:main"` (import package is literally `src`)
- `[tool.hatch.build.targets.wheel] packages = ["src"]`
- `[tool.hatch.build.targets.wheel.force-include]` maps repo-root `config.toml` -> `config.toml` and `skills` -> `skills` (wheel root, NOT inside a package)
- `[tool.hatch.build.targets.sdist] include = ["src","skills","config.toml","README.md"]`

Runtime resolution call sites that break after rename (all use `__file__` walk-up, NOT importlib):
- `src/main.py:135-136` — `PROJECT_ROOT = Path(__file__).resolve().parent.parent` then `DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"`
- `src/planning/skills.py:32-50` — `resolve_skills_dir()`: env override `BAREAGENT_SKILLS_DIR`, else `Path(__file__).resolve()` then candidates `parents[2]/"skills"` and `parents[1]/"skills"`, returns first that `.exists()`
- (also `src/debug/web_viewer.py:12` uses `Path(__file__).with_name("viewer.html")` — that one is a true package-adjacent data file and stays correct after rename, but is a candidate for the same importlib.resources treatment.)

Repo state relevant to hatch-vcs pitfalls (verified live):
- Existing git tag: `backup-before-email-rewrite` (a NON-version tag — see Pitfalls, tag-pattern). No `vX.Y.Z` tags yet.
- `git describe --tags` -> `backup-before-email-rewrite-127-g268b2a8` (confirms the stray tag is what describe currently latches onto).

---

## Q1 — hatch-vcs (version from git tags)

Source: https://github.com/ofek/hatch-vcs (README), https://pypi.org/project/hatch-vcs/ (v0.5.0 current), https://context7.com/ofek/hatch-vcs/llms.txt

### Required pyproject changes

```toml
[build-system]
requires = ["hatchling>=1.27", "hatch-vcs>=0.4"]
build-backend = "hatchling.build"

[project]
name = "bareagent"
dynamic = ["version"]          # remove the hardcoded version = "0.1.0"
# ... rest of metadata ...

[tool.hatch.version]
source = "vcs"
```

Three coupled edits:
1. Add `hatch-vcs` to `[build-system] requires`.
2. In `[project]`, DELETE `version = "..."` and add `dynamic = ["version"]`. (Hatchling errors if `version` is both static and dynamic; the field must be one or the other.)
3. Add `[tool.hatch.version]` with `source = "vcs"` (the version-source plugin is named `vcs`).

### How the version is computed from tags

hatch-vcs is a thin Hatchling wrapper around `setuptools-scm`. Computation:
- **On an exact tag** `vX.Y.Z` -> version is `X.Y.Z`. The default `tag-pattern` strips a leading `v` (it captures the numeric part of the tag), so a tag `v1.2.3` produces `1.2.3`. (No `tag = "v{version}"` config needed for the standard `vX.Y.Z` convention — the default pattern handles the `v` prefix.)
- **On an un-tagged / dirty commit** (typical local dev): setuptools-scm produces a PEP 440 dev version that GUESSES THE NEXT release by default, e.g. `1.2.4.dev3+g268b2a8` (= next patch `.devN` where N = commits since tag, `+g<shorthash>` local segment; a dirty tree appends `.dYYYYMMDD`). The crucial gotcha is it bumps to the *next* version, not the current tag.
- To NOT guess the next version on non-release commits, pass setuptools-scm raw options:
  ```toml
  [tool.hatch.version.raw-options]
  version_scheme = "no-guess-dev"   # produces e.g. 1.2.3.post3+g268b2a8 instead of 1.2.4.devN
  ```
  (Optional; the default guess-next behavior is fine and conventional. Document the choice either way.)

### Useful safety / config options on `[tool.hatch.version]`

| Option | Purpose |
|---|---|
| `fallback-version = "0.0.0"` | Version used when VCS detection fails (no `.git`, no tags). WITHOUT this, a failed detection RAISES an error and the build/install fails. Strongly recommended for sdist-from-tarball builds and CI robustness. |
| `tag-pattern = "..."` | Regex to extract version from tags. Override the default if you have stray non-version tags (see Pitfalls). Pattern must contain one match group OR a group named `version`. |
| `raw-options = { ... }` | Pass-through table of setuptools-scm params (`version_scheme`, `local_scheme`, `root`, `search_parent_directories`, etc.). `write_to`/`write_to_template` are ignored here (use the build hook instead). |

### Exposing `__version__` at runtime (the vcs build hook)

The `[tool.hatch.build.hooks.vcs]` build hook writes a generated `_version.py` into the source tree during build:

```toml
[tool.hatch.build.hooks.vcs]
version-file = "src/bareagent/_version.py"
```

- `version-file` is REQUIRED for the hook; path is relative to project root. Put it INSIDE the package (`src/bareagent/_version.py`) so it ships in the wheel.
- Generated file content (setuptools-scm template):
  ```python
  # file generated by setuptools_scm
  # don't change, don't track in version control
  __version__ = version = '0.1.0.dev3+g268b2a8'
  __version_tuple__ = version_tuple = (0, 1, 0, 'dev3', 'g268b2a8')
  ```
- IMPORTANT: this file must be git-ignored (it is regenerated each build; tracking it causes churn / dirty-tree version noise). Add `src/bareagent/_version.py` to `.gitignore`.
- It is NOT created until a build runs. To generate it without producing artifacts: `hatch build --hooks-only` (or `python -m build` / `uv build`).
- Consume at runtime with a fallback so a fresh checkout that never built still imports:
  ```python
  try:
      from bareagent._version import __version__
  except ImportError:  # not built yet (fresh source checkout, no hook run)
      __version__ = "0.0.0+unknown"
  ```

### Recommended runtime alternative to the build hook: importlib.metadata

The simplest, hook-free way to expose `__version__` at runtime is to read the INSTALLED distribution metadata (works for both wheel installs and editable installs, no generated file to gitignore):

```python
# src/bareagent/__init__.py
from importlib.metadata import PackageNotFoundError, version
try:
    __version__ = version("bareagent")
except PackageNotFoundError:   # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"
```

Trade-off: `importlib.metadata.version()` returns the version recorded AT INSTALL TIME. In an editable install it can go stale after new commits/tags until you reinstall. The vcs build hook `_version.py` is also static post-build (same staleness in editable installs). For a CLI distributed via `pipx`/wheel, `importlib.metadata` is the cleaner choice (no `.gitignore` entry, no hook). Pick ONE; don't need both.

---

## Q2 — src-layout in hatchling

Source: https://hatch.pypa.io/latest/config/build/ (Build configuration), https://hatch.pypa.io/latest/plugins/builder/wheel/ (Wheel builder)

### Wheel target

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/bareagent"]
```

- The `packages` option is semantically equivalent to `only-include` EXCEPT the shipped path is collapsed to its final component. So `packages = ["src/bareagent"]` ships the dir `src/bareagent/` AS `bareagent/` in the wheel (the `src/` prefix is stripped). Internally it expands to:
  ```toml
  [tool.hatch.build.targets.wheel]
  only-include = ["src/bareagent"]
  sources = ["src"]   # rewrites the relative path: strips the src/ prefix
  ```
- This is the canonical src-layout config: the import package becomes `bareagent` (NOT `src.bareagent`), so the entry point becomes `bareagent = "bareagent.main:main"`.

### Auto-detection (can you omit `packages`?)

The wheel builder has a "Default file selection" heuristic when NO file-selection options are set: it uses the project NAME to find the package, in this order:
1. `<name>/__init__.py`
2. `src/<name>/__init__.py`   <- src-layout is heuristic #2
3. `<name>.py`
4. `<name>/<name>/__init__.py`

So with `name = "bareagent"` and the source at `src/bareagent/__init__.py`, hatchling WOULD auto-detect it and you could omit `packages` entirely. HOWEVER: being explicit with `packages = ["src/bareagent"]` is the recommended best practice — it's unambiguous, documents intent, and is required the moment you add any OTHER file-selection option (because once you set any selection option the auto-heuristic no longer fires). Keep it explicit.

(If auto-detection fails and no options are set, hatchling raises an error; `bypass-selection = true` suppresses that error but is not what you want here.)

### sdist target — how it differs

- The sdist builder's "Default file selection": when NO selection options are set, it includes ALL files NOT ignored by your VCS (i.e., everything `git` tracks, respecting `.gitignore`). This is fundamentally different from the wheel (which ships only the package).
- Always-included regardless of options (cannot be excluded): `/pyproject.toml`, `/hatch.toml`, `/hatch_build.py`, `/.gitignore`, the declared `readme` file, and all declared `license-files`.
- Practical implication: the sdist naturally contains `src/`, `tests/`, `README.md`, `LICENSE`, etc. as long as they are git-tracked. Because `config.toml` and `skills/` will be MOVED into `src/bareagent/` (git-tracked, not gitignored), they land in the sdist automatically with NO explicit sdist config. You can keep a small explicit `[tool.hatch.build.targets.sdist] include = [...]` for clarity, but it's optional once VCS-default selection covers it.
- IMPORTANT for hatch-vcs sdists: the sdist does NOT carry `.git/`. hatch-vcs writes the resolved version into the sdist's `PKG-INFO` metadata at build time, so building a wheel FROM the sdist reads the version from `PKG-INFO` (no git needed). But building a wheel from a git-less, PKG-INFO-less tree fails unless `fallback-version` is set.

---

## Q3 — Bundling package data (config.toml + skills/ tree)

Source: same Build configuration docs + sdist/wheel builder docs.

### Best practice: put data INSIDE the package dir (not force-include to wheel root)

After migration the files live at:
```
src/bareagent/config.toml
src/bareagent/skills/code-review/SKILL.md
src/bareagent/skills/git/SKILL.md
src/bareagent/skills/test/SKILL.md
... (skills tree)
```

With `packages = ["src/bareagent"]`, hatchling ships the WHOLE `src/bareagent/` directory tree, and **non-`.py` files inside the package directory ARE auto-included** — hatchling does NOT filter by extension the way old setuptools `package_data` did. There is no `include_package_data` / `package_data` equivalent needed; a file living under the selected package path ships by default. So `config.toml` and the entire `skills/` subtree are bundled automatically into BOTH wheel and sdist with NO extra config, purely by virtue of being inside `src/bareagent/`.

This makes the current `[tool.hatch.build.targets.wheel.force-include]` block OBSOLETE — DELETE it after the move. force-include/artifacts are only needed for files that live OUTSIDE the package dir or are git-ignored:
- `artifacts` — for VCS-ignored files (e.g. compiled `*.so`, OR a gitignored generated `_version.py` if you used the build hook and it sits in the package — though by default the wheel ships everything in the package path; gitignored files are excluded from the wheel selection too, so a gitignored `_version.py` needs `artifacts = ["src/bareagent/_version.py"]` to be re-included). Semantically equivalent to `include`.
- `force-include` — maps arbitrary filesystem paths to distribution paths; only needed when data is NOT inside the package.

### Caveat on VCS-ignore interaction

The wheel builder still respects `.gitignore` for file selection. As long as `src/bareagent/config.toml` and `src/bareagent/skills/**` are git-TRACKED (not ignored), they ship. (`config.local.toml` stays gitignored and outside the package, so it is correctly NEVER bundled — preserving the security posture noted in the PRD.) If you adopt the vcs build hook and gitignore `_version.py`, re-add it via `artifacts = ["src/bareagent/_version.py"]`.

### Recommended config block

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/bareagent"]
# config.toml + skills/** ride along automatically because they live inside src/bareagent/
# (delete the old [tool.hatch.build.targets.wheel.force-include] block)

# Only if using the vcs build hook with a gitignored _version.py:
# artifacts = ["src/bareagent/_version.py"]

[tool.hatch.build.targets.sdist]
# Optional/explicit; VCS-default selection already covers git-tracked files.
include = ["src", "tests", "README.md", "LICENSE"]
```

---

## Q4 — Runtime location via importlib.resources (Python 3.12)

Source: https://docs.python.org/3.12/library/importlib.resources.html

### The modern pattern (replaces `Path(__file__).parent.parent / "config.toml"`)

Single file:
```python
from importlib.resources import files
# Traversable to the bundled config.toml
cfg_traversable = files("bareagent") / "config.toml"
text = cfg_traversable.read_text(encoding="utf-8")   # if read_text suffices, you're done
```

`files(anchor)` returns a `Traversable` (think directory handle) for the package; `/` (joinpath) navigates into it; `.read_text()` / `.read_bytes()` read leaves. This works identically for editable installs (resolves to the real source file) and wheel installs (resolves inside site-packages), and even zipped imports.

### When you need a real `pathlib.Path` (e.g. code that passes the path elsewhere)

Use `as_file`, which yields a context manager providing a real filesystem `Path` (extracting to a temp file/dir only if the resource lives in a zip):
```python
from importlib.resources import files, as_file

resource = files("bareagent") / "config.toml"
with as_file(resource) as config_path:   # config_path: pathlib.Path
    load_config(config_path)
    # NOTE: for zip-imported packages the temp file is cleaned up on context exit,
    # so do the work INSIDE the with-block; don't stash the Path for later.
```
- For a normal (non-zip) install, `as_file` returns the actual on-disk path with no copy. For zipped installs it materializes a temp copy. Since BareAgent will be a normal wheel/editable install (not a zipapp), `as_file` is essentially free, but using it keeps the code correct even if someone zip-imports.
- The DEPRECATED `importlib.resources.path(pkg, name)` is exactly `as_file(files(pkg).joinpath(name))` — use `as_file` instead.

### Iterating a bundled directory tree (the `skills/` case)

Python 3.12 added directory support to `as_file` (was file-only before). Two approaches:

(a) Traversable iteration (no temp extraction; preferred for reading):
```python
from importlib.resources import files

skills_root = files("bareagent") / "skills"   # Traversable directory
for entry in skills_root.iterdir():            # one level; entry is a Traversable
    if entry.is_dir():
        skill_md = entry / "SKILL.md"
        if skill_md.is_file():
            content = skill_md.read_text(encoding="utf-8")
```
`iterdir()` lists one level (does not recurse). For BareAgent's `skills/<name>/SKILL.md` layout this is exactly right — iterate top-level skill dirs, then `entry / "SKILL.md"`.

(b) Get a real directory `Path` when existing code wants to `Path.glob("*/SKILL.md")` (3.12+, directory support in `as_file`):
```python
from importlib.resources import files, as_file

with as_file(files("bareagent") / "skills") as skills_dir:  # skills_dir: pathlib.Path
    for skill_md in skills_dir.glob("*/SKILL.md"):
        ...
```

### Clean replacement for the two current call sites

- `src/main.py` `DEFAULT_CONFIG_PATH`: replace `PROJECT_ROOT / "config.toml"` with the `as_file(files("bareagent") / "config.toml")` pattern (or read directly with `.read_text()` if the consumer can take text/bytes). Note: keep the existing env/CLI override (`BAREAGENT_CONFIG` / `--config`) ahead of the bundled default; the importlib path is only the FALLBACK default.
- `src/planning/skills.py` `resolve_skills_dir()`: replace the `Path(__file__).parents[2]/"skills"` candidates with `files("bareagent") / "skills"` (keep the `BAREAGENT_SKILLS_DIR` env override first). Since downstream code does `.glob("*/SKILL.md")` and stores a `Path`, use approach (b) `as_file` to keep returning a `Path` — but mind the lifetime caveat below (Pitfalls).

Both replacements work identically in `uv pip install -e .` (editable -> real source dir) and in an installed wheel (-> site-packages), which the current `__file__` walk-up does NOT once the package is named `bareagent` and data lives inside it.

---

## Q5 — Pitfalls

### hatch-vcs + editable installs / CI

1. **Needs git history + tags present.** hatch-vcs computes the version by shelling out to git in the project root. If `.git` is absent (e.g. building from a downloaded source tree without VCS metadata) or there are zero tags, version detection FAILS and raises — UNLESS you set `fallback-version`. Set `fallback-version` defensively.
2. **CI shallow clone breaks versioning.** GitHub Actions `actions/checkout` defaults to `fetch-depth: 1` (shallow, no tags). hatch-vcs then can't see the tag and either fails or produces a wrong dev version. FIX: in the release workflow, `actions/checkout` with `fetch-depth: 0` (full history + tags). This is the #1 hatch-vcs CI gotcha and directly affects this task's tag-triggered release workflow.
3. **Stray non-version tags poison `git describe`.** This repo ALREADY has a tag `backup-before-email-rewrite` and `git describe --tags` currently returns `backup-before-email-rewrite-127-g268b2a8`. The default `tag-pattern` only extracts a version from tags that LOOK like versions, so a non-numeric tag is typically skipped — but to be safe and deterministic, either (a) set an explicit `tag-pattern` that only matches `v?\d+\.\d+\.\d+`, e.g. `tag-pattern = "^v?(?P<version>\\d+\\.\\d+\\.\\d+)$"`, or (b) ensure the first real release tag `vX.Y.Z` is the most recent reachable version-shaped tag. Verify with `hatch version` (or `python -m setuptools_scm`) BEFORE relying on CI.
4. **Editable-install version staleness.** Whether you use the `_version.py` build hook or `importlib.metadata`, the version recorded in an editable install is captured AT INSTALL/BUILD time and does NOT auto-update as you commit/tag. `importlib.metadata.version()` reads the `.dist-info` written at install; the build-hook `_version.py` is static post-build. Re-run install/build to refresh. For a CLI this rarely matters; just don't expect `pip install -e .` to track new tags live.
5. **Dirty tree appends a date/local segment** (e.g. `+d20260614` or `.dYYYYMMDD`), which is NOT a valid PyPI upload version — but that only matters for accidental uploads from a dirty tree; CI builds from a clean tagged checkout produce a clean `X.Y.Z`. Tag-triggered + clean checkout avoids this.
6. **`_version.py` must be gitignored** if you use the build hook (it's regenerated each build; tracking it creates churn and can make the tree "dirty"). And if gitignored AND inside the package, re-include it in the wheel via `artifacts = ["src/bareagent/_version.py"]` (gitignored files are excluded from wheel selection by default).

### importlib.resources with directories

7. **Directory support in `as_file` is 3.12+.** `as_file` on a Traversable DIRECTORY only works on Python 3.12+. Project is 3.12+ so this is fine, but it's the reason the directory-`Path` pattern (Q4b) is gated to 3.12.
8. **`as_file` lifetime for directories.** For a normal install `as_file` returns the real path with no copy, but for zip/non-filesystem loaders it extracts to a TEMP directory cleaned up on context-manager exit. So `resolve_skills_dir()` returning a `Path` derived from `as_file` and used LATER (outside the `with`) is unsafe in the zip case. For a normal wheel/editable install it's safe (real path). Safest refactor: do skill scanning INSIDE the `with as_file(...)` block, OR switch the scanner to Traversable iteration (Q4a) which needs no temp extraction at all. Current code stores the `Path` and uses it later, so prefer Traversable iteration to avoid the lifetime trap.
9. **`iterdir()` is single-level, not recursive.** Matches the `skills/<name>/SKILL.md` shape (iterate skill dirs, then join `SKILL.md`); for deeper trees you'd recurse manually.
10. **`files(anchor)` anchors to the IMPORT package name.** After migration use `files("bareagent")`, NOT `files("src")` and NOT `files(__name__)` unless `__name__` is right. The package must be importable (installed or on path) for `files()` to resolve — true for both editable and wheel installs.

### Exposing `__version__`

11. **Pick one source, with a fallback.** Either `importlib.metadata.version("bareagent")` (no gitignore/hook, cleanest for a CLI) OR the vcs build-hook `_version.py` (works without install, needs gitignore + artifacts). Both need a try/except fallback for the "source tree never built/installed" case so `import bareagent` never crashes.
12. **`dynamic = ["version"]` is mandatory and mutually exclusive with static `version`.** Forgetting to remove `version = "0.1.0"` while adding `dynamic = ["version"]` is a hard build error.

---

## Copy-pasteable target pyproject snippet (synthesis)

```toml
[build-system]
requires = ["hatchling>=1.27", "hatch-vcs>=0.4"]
build-backend = "hatchling.build"

[project]
name = "bareagent"
dynamic = ["version"]
# ... description, readme, license, authors, classifiers, requires-python, dependencies ...

[project.scripts]
bareagent = "bareagent.main:main"     # was src.main:main

[tool.hatch.version]
source = "vcs"
fallback-version = "0.0.0"            # robustness when git/tags absent

# Optional — only if you want __version__ written into the package at build time
# (otherwise prefer importlib.metadata in src/bareagent/__init__.py):
# [tool.hatch.build.hooks.vcs]
# version-file = "src/bareagent/_version.py"

[tool.hatch.build.targets.wheel]
packages = ["src/bareagent"]
# config.toml + skills/** are auto-included because they live inside src/bareagent/.
# Delete the old [tool.hatch.build.targets.wheel.force-include] block.
# artifacts = ["src/bareagent/_version.py"]   # only if using the gitignored build hook

[tool.hatch.build.targets.sdist]
include = ["src", "tests", "README.md", "LICENSE"]   # optional; VCS default already covers tracked files
```

Recommended `src/bareagent/__init__.py` (hook-free version exposure):
```python
from importlib.metadata import PackageNotFoundError, version
try:
    __version__ = version("bareagent")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
```

Verify before trusting CI: `uv build` then `twine check dist/*`; `hatch version` (or inspect wheel name) should print the tag-derived version; confirm `backup-before-email-rewrite` tag does not leak into the version.

---

## External References

- hatch-vcs README (canonical config, version source / build hook / metadata hook options, tag-pattern, fallback-version, raw-options, write-version-file): https://github.com/ofek/hatch-vcs
- hatch-vcs on PyPI (v0.5.0 current): https://pypi.org/project/hatch-vcs/
- hatch-vcs llms.txt (raw-options examples, version_scheme/local_scheme, _version.py consumption): https://context7.com/ofek/hatch-vcs/llms.txt
- Hatch — Build configuration (packages / only-include / sources / force-include / artifacts / VCS file selection): https://hatch.pypa.io/latest/config/build/
- Hatch — Wheel builder (default file selection heuristic incl. src-layout): https://hatch.pypa.io/latest/plugins/builder/wheel/
- Hatch — sdist builder (default = all VCS-tracked files; always-included files): https://github.com/pypa/hatch/blob/master/docs/plugins/builder/sdist.md
- Python 3.12 importlib.resources (files / as_file w/ 3.12 directory support / iterdir / deprecation of path & contents): https://docs.python.org/3.12/library/importlib.resources.html

## Related Specs

- `.trellis/tasks/06-14-pypi-tag-ci/prd.md` — the parent PRD (decisions locked: hatch-vcs, src-layout option A, MIT license, tag-triggered Trusted Publishing CI). This research backs the "改用 hatch-vcs" + "config.toml/skills 移入包内走 importlib.resources" requirements.

## Caveats / Not Found

- Exact MINIMUM `hatch-vcs` version pin is a judgment call; `>=0.4` is safe (0.4+ tracks modern hatchling; current is 0.5.0). Build-hook `_version.py` works from 0.3.0+. Pin to taste; not security-sensitive.
- `tag-pattern` default regex source was not read line-by-line; the README states it strips a leading `v` and captures the version. The explicit `^v?(?P<version>\d+\.\d+\.\d+)$` shown above is a defensive override, NOT a verbatim copy of the default — validate with `hatch version` on a real `vX.Y.Z` tag before depending on it (esp. given the stray `backup-before-email-rewrite` tag in this repo).
- I did NOT modify any code or pyproject.toml (research-only). The "replacement" snippets are proposals for the implement agent, not applied changes.
- GitHub Actions / Trusted Publishing (OIDC) workflow specifics are out of scope for THIS file (separate research topic in the same task); only the `fetch-depth: 0` checkout pitfall is noted here because it directly couples to hatch-vcs.
