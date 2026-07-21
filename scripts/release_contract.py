#!/usr/bin/env python3
"""Validate release refs and the exact wheel/sdist pair produced by hatch-vcs."""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

_SEMVER_NUMBER = r"(?:0|[1-9][0-9]*)"
_VERSION_TAG = re.compile(rf"v(?P<version>{_SEMVER_NUMBER}\.{_SEMVER_NUMBER}\.{_SEMVER_NUMBER})\Z")


@dataclass(frozen=True, slots=True)
class DistributionSet:
    """The one wheel and one sdist allowed to continue to publishing."""

    wheel: Path
    sdist: Path
    version: str


def strict_tag_version(tag: str) -> str:
    """Return the version from a strict ``vMAJOR.MINOR.PATCH`` tag."""
    match = _VERSION_TAG.fullmatch(tag)
    if match is None:
        raise ValueError(f"tag must use strict vMAJOR.MINOR.PATCH syntax: {tag!r}")
    return match.group("version")


def expected_version_for_ref(event_name: str, ref_type: str, ref_name: str) -> str | None:
    """Resolve a release's expected version, rejecting unsafe event/ref pairs."""
    if event_name == "push":
        if ref_type != "tag":
            raise ValueError("a release push must use a tag ref")
        return strict_tag_version(ref_name)
    if event_name != "workflow_dispatch":
        raise ValueError(f"unsupported release event: {event_name!r}")
    if ref_type == "tag":
        return strict_tag_version(ref_name)
    if ref_type == "branch":
        return None
    raise ValueError(f"workflow_dispatch must use a branch or tag ref, got {ref_type!r}")


def _metadata_version(contents: bytes, source: Path) -> str:
    message = BytesParser(policy=policy.default).parsebytes(contents)
    version = message.get("Version")
    if not version:
        raise ValueError(f"distribution metadata has no Version field: {source}")
    return version


def _wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            raise ValueError(f"wheel must contain exactly one .dist-info/METADATA file: {wheel}")
        return _metadata_version(archive.read(metadata_files[0]), wheel)


def _sdist_version(sdist: Path) -> str:
    with tarfile.open(sdist, "r:gz") as archive:
        metadata_files = [
            member
            for member in archive.getmembers()
            if member.isfile()
            and member.name.endswith("/PKG-INFO")
            and len(PurePosixPath(member.name).parts) == 2
        ]
        if len(metadata_files) != 1:
            raise ValueError(f"sdist must contain exactly one top-level PKG-INFO file: {sdist}")
        extracted = archive.extractfile(metadata_files[0])
        if extracted is None:
            raise ValueError(f"could not read sdist PKG-INFO: {sdist}")
        return _metadata_version(extracted.read(), sdist)


def validate_distributions(dist_dir: Path, expected_version: str | None = None) -> DistributionSet:
    """Require exactly one wheel and one sdist with matching metadata versions."""
    if not dist_dir.is_dir():
        raise ValueError(f"distribution directory does not exist: {dist_dir}")

    entries = sorted(dist_dir.iterdir(), key=lambda path: path.name)
    wheels = [path for path in entries if path.is_file() and path.name.endswith(".whl")]
    sdists = [path for path in entries if path.is_file() and path.name.endswith(".tar.gz")]
    if len(entries) != 2 or len(wheels) != 1 or len(sdists) != 1:
        inventory = ", ".join(path.name for path in entries) or "(empty)"
        raise ValueError(f"dist must contain exactly one wheel and one sdist; found: {inventory}")

    wheel_version = _wheel_version(wheels[0])
    sdist_version = _sdist_version(sdists[0])
    if wheel_version != sdist_version:
        raise ValueError(f"wheel and sdist versions differ: {wheel_version!r} != {sdist_version!r}")
    if expected_version is not None and wheel_version != expected_version:
        raise ValueError(
            f"built version {wheel_version!r} does not match expected {expected_version!r}"
        )
    return DistributionSet(wheel=wheels[0], sdist=sdists[0], version=wheel_version)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--ref-type", required=True)
    parser.add_argument("--ref-name", required=True)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument(
        "--ref-only",
        action="store_true",
        help="validate only the event/ref pair before building distributions",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        expected = expected_version_for_ref(args.event_name, args.ref_type, args.ref_name)
        if args.ref_only:
            print(expected or "dynamic")
            return 0
        distributions = validate_distributions(args.dist_dir, expected_version=expected)
    except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError) as exc:
        print(f"release contract failed: {exc}", file=sys.stderr)
        return 1
    print(distributions.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
