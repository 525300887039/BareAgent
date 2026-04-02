from __future__ import annotations

from typing import Any


def parse_permission_rules(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Parse allow and deny permission rules from config data."""
    permission_config = config.get("permission", {})
    allow = _coerce_rule_list(permission_config.get("allow", []))
    deny = _coerce_rule_list(permission_config.get("deny", []))
    return allow, deny


def _coerce_rule_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(rule) for rule in value]
