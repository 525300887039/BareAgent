#!/usr/bin/env python3
"""Verify runtime resources from an installed BareAgent distribution."""

from __future__ import annotations

import sys
from importlib.resources import files

import bareagent

_BUILTIN_SKILLS = ("code-review", "git", "test")


def main() -> int:
    package = files("bareagent")
    required = [package.joinpath("config.toml")]
    required.extend(package.joinpath("skills", skill, "SKILL.md") for skill in _BUILTIN_SKILLS)
    missing = [str(resource) for resource in required if not resource.is_file()]
    if missing:
        print("installed package is missing runtime resources:", file=sys.stderr)
        for resource in missing:
            print(f"- {resource}", file=sys.stderr)
        return 1

    print(f"bareagent import: {bareagent.__file__}")
    print("runtime resources: config.toml + built-in skills OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
