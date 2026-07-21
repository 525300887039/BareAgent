"""Behavioral and static regression tests for the release contract."""

from __future__ import annotations

import io
import re
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.release_contract import (
    expected_version_for_ref,
    strict_tag_version,
    validate_distributions,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def _command_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _job(workflow: str, name: str) -> str:
    """Return one top-level job block without adding a YAML dependency."""
    lines = workflow.splitlines()
    start = next(i for i, line in enumerate(lines) if line == f"  {name}:")
    end = next(
        (i for i in range(start + 1, len(lines)) if re.fullmatch(r"  [A-Za-z0-9_-]+:", lines[i])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _permissions(job: str) -> list[str]:
    match = re.search(r"(?m)^    permissions:\n((?:      [^\n]+\n?)*)", job)
    assert match is not None
    return [line.strip() for line in match.group(1).splitlines() if line.strip()]


@pytest.mark.parametrize("tag", ["v0.2.0", "v1.0.0", "v10.23.456"])
def test_strict_tag_accepts_release_semver(tag: str) -> None:
    assert strict_tag_version(tag) == tag.removeprefix("v")


@pytest.mark.parametrize(
    "tag",
    [
        "v*",
        "0.2.0",
        "vv0.2.0",
        "v0.2",
        "v0.2.0.1",
        "v0.2.0-rc.1",
        "v0.2.0+build",
        "v00.2.0",
        "v0.02.0",
        "v0.2.00",
        "v0.2.0\n",
    ],
)
def test_strict_tag_rejects_non_release_semver(tag: str) -> None:
    with pytest.raises(ValueError, match="strict vMAJOR.MINOR.PATCH"):
        strict_tag_version(tag)


def test_expected_version_requires_a_strict_tag_for_push() -> None:
    assert expected_version_for_ref("push", "tag", "v0.2.0") == "0.2.0"
    with pytest.raises(ValueError, match="tag ref"):
        expected_version_for_ref("push", "branch", "main")


def test_workflow_dispatch_allows_branch_or_strict_tag() -> None:
    assert expected_version_for_ref("workflow_dispatch", "branch", "main") is None
    assert expected_version_for_ref("workflow_dispatch", "tag", "v0.2.0") == "0.2.0"
    with pytest.raises(ValueError, match="strict vMAJOR.MINOR.PATCH"):
        expected_version_for_ref("workflow_dispatch", "tag", "v0.2.0-rc.1")


def _write_distributions(dist: Path, wheel_version: str, sdist_version: str) -> None:
    dist.mkdir()
    wheel = dist / f"bareagent_cli-{wheel_version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"bareagent_cli-{wheel_version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: bareagent-cli\nVersion: {wheel_version}\n",
        )

    sdist = dist / f"bareagent_cli-{sdist_version}.tar.gz"
    metadata = (f"Metadata-Version: 2.4\nName: bareagent-cli\nVersion: {sdist_version}\n").encode()
    with tarfile.open(sdist, "w:gz") as archive:
        member = tarfile.TarInfo(f"bareagent_cli-{sdist_version}/PKG-INFO")
        member.size = len(metadata)
        archive.addfile(member, io.BytesIO(metadata))


def test_distribution_contract_accepts_one_matching_wheel_and_sdist(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    _write_distributions(dist, "0.2.0", "0.2.0")

    result = validate_distributions(dist, expected_version="0.2.0")

    assert result.version == "0.2.0"
    assert result.wheel.suffix == ".whl"
    assert result.sdist.name.endswith(".tar.gz")


def test_distribution_contract_rejects_an_old_extra_artifact(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    _write_distributions(dist, "0.2.0", "0.2.0")
    (dist / "bareagent_cli-0.1.0-py3-none-any.whl").write_bytes(b"old")

    with pytest.raises(ValueError, match="exactly one wheel and one sdist"):
        validate_distributions(dist)


def test_distribution_contract_rejects_mismatched_metadata(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    _write_distributions(dist, "0.2.0", "0.2.1")

    with pytest.raises(ValueError, match="versions differ"):
        validate_distributions(dist)


def test_distribution_contract_rejects_tag_version_mismatch(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    _write_distributions(dist, "0.2.0", "0.2.0")

    with pytest.raises(ValueError, match="does not match expected"):
        validate_distributions(dist, expected_version="0.2.1")


def test_release_reuses_the_complete_ci_workflow() -> None:
    ci = _read(".github/workflows/ci.yml")
    release = _read(".github/workflows/release.yml")
    quality = _job(release, "quality")
    ci_commands = _command_lines(ci)

    assert "workflow_call:" in ci
    assert "uses: ./.github/workflows/ci.yml" in quality
    for command in (
        "uv run ruff check src tests",
        "uv run ruff format --check src tests",
        "uv run pyright",
        "uv run pytest",
        "uv run pytest -m socket",
    ):
        assert command in ci_commands


def test_publish_jobs_explicitly_need_every_gate() -> None:
    release = _read(".github/workflows/release.yml")
    required = "needs: [quality, build, smoke-wheel, smoke-sdist]"
    assert required in _job(release, "publish-to-pypi")
    assert required in _job(release, "publish-to-testpypi")


def test_release_validates_before_uploading_artifacts() -> None:
    build = _job(_read(".github/workflows/release.yml"), "build")
    assert "rm -rf dist" in build
    assert "uv build --out-dir dist --no-create-gitignore" in build
    assert "--dist-dir dist" in build
    build_index = build.index("uv build --out-dir dist --no-create-gitignore")
    contract_index = build.index("--dist-dir dist")
    twine_index = build.index("uvx twine check dist/*.whl dist/*.tar.gz")
    upload_index = build.index("actions/upload-artifact@")
    assert build_index < contract_index < twine_index < upload_index
    assert "uvx twine check dist/*.whl dist/*.tar.gz" in build
    assert "dist/*.whl" in build
    assert "dist/*.tar.gz" in build
    assert "path: dist/" not in build


def test_wheel_smoke_installs_the_built_wheel_not_editable_source() -> None:
    smoke = _job(_read(".github/workflows/release.yml"), "smoke-wheel")
    commands = _command_lines(smoke)
    assert commands.count("uv pip install") == 1
    assert "uv pip install --python .smoke-wheel/bin/python dist/*.whl" in commands
    assert "smoke_installed_package.py" in commands
    assert "bareagent --help" in commands
    assert not re.search(r"(?:^|\s)-e(?:\s|$)", commands)


def test_sdist_smoke_installs_the_built_sdist() -> None:
    smoke = _job(_read(".github/workflows/release.yml"), "smoke-sdist")
    commands = _command_lines(smoke)
    assert commands.count("uv pip install") == 1
    assert "uv pip install --python .smoke-sdist/bin/python dist/*.tar.gz" in commands
    assert "smoke_installed_package.py" in commands
    assert "bareagent --help" in commands
    assert not re.search(r"(?:^|\s)-e(?:\s|$)", commands)


def test_only_publish_jobs_receive_oidc_and_no_extra_permissions() -> None:
    release = _read(".github/workflows/release.yml")
    workflow_header = release.split("\njobs:", maxsplit=1)[0]
    assert re.search(r"(?m)^permissions: \{\}$", workflow_header)
    assert "id-token:" not in workflow_header

    for name in ("publish-to-pypi", "publish-to-testpypi"):
        job = _job(release, name)
        assert _permissions(job) == ["id-token: write"]

    for name in ("quality", "build", "smoke-wheel", "smoke-sdist"):
        assert _permissions(_job(release, name)) == ["contents: read"]


def test_release_serializes_the_same_ref_without_cancelling() -> None:
    release = _read(".github/workflows/release.yml")
    assert "concurrency:" in release
    assert "github.ref" in release
    assert "cancel-in-progress: false" in release


def test_external_actions_are_pinned_to_commits() -> None:
    for workflow_name in ("ci.yml", "release.yml"):
        for line in _read(f".github/workflows/{workflow_name}").splitlines():
            stripped = line.strip()
            if not stripped.startswith("uses: "):
                continue
            action = stripped.removeprefix("uses: ").split(" #", maxsplit=1)[0]
            if action.startswith("./"):
                continue
            assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action), line


def test_testpypi_does_not_mask_duplicate_versions() -> None:
    job = _job(_read(".github/workflows/release.yml"), "publish-to-testpypi")
    assert "skip-existing" not in job


def test_sdist_contains_release_contract_layout() -> None:
    pyproject = tomllib.loads(_read("pyproject.toml"))
    include = set(pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["include"])
    assert "scripts" in include
    assert ".github/workflows/ci.yml" in include
    assert ".github/workflows/release.yml" in include
    assert "CHANGELOG.md" in include
