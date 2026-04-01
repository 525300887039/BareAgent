from __future__ import annotations

from typing import Any


def parse_permission_rules(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Parse allow and deny permission rules from config data."""
    permission_config = config.get("permission", {})
    allow = permission_config.get("allow", [])
    deny = permission_config.get("deny", [])
    return [str(rule) for rule in allow], [str(rule) for rule in deny]
