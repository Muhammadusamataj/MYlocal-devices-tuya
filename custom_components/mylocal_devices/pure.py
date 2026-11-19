"""Pure helpers shared by the platforms. No Home Assistant imports here."""

from __future__ import annotations

import re

BOOL_TYPES = {"bool", "boolean"}
_EXACT_SWITCH = re.compile(
    r"^(switch|switch_\d+|power|power_switch|relay|relay_\d+|onoff|on_off|main_switch)$"
)
_NUMERIC_SWITCH = re.compile(r"^(switch|power|relay)_\d+$")
_DEPRIORITISED = re.compile(
    r"prepay|prepayment|credit|balance|energy|child_lock|leak|test|alarm|fault|online"
)


def point_type(point: dict) -> str:
    dtype = str(point.get("type") or "").strip().lower()
    if dtype:
        return dtype
    value = point.get("cloud_value")
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, str):
        return "string"
    return ""


def is_bool_point(point: dict) -> bool:
    return point_type(point) in BOOL_TYPES


def find_addon(addons: list[dict], slug: str, name: str = "") -> str | None:
    """Find the installed Supervisor slug for our add-on.

    Custom repositories prefix the configured slug with a repository hash, so
    `tuya_local_key` can be installed as e.g. `a1b2c3_tuya_local_key`.
    """
    for addon in addons:
        if not isinstance(addon, dict):
            continue
        installed = str(addon.get("slug") or "").strip()
        if installed == slug or installed.endswith(f"_{slug}"):
            return installed
        if name and str(addon.get("name") or "").strip() == name:
            return installed
    return None


def main_score(point: dict) -> int:
    if not is_bool_point(point):
        return 90
    code = str(point.get("code") or "").lower()
    if _EXACT_SWITCH.match(code):
        return 0
    if _NUMERIC_SWITCH.match(code):
        return 1
    if _DEPRIORITISED.search(code):
        return 80
    if re.search(r"switch|power|relay|onoff|on_off", code):
        return 20
    return 40


def primary_point(points: list[dict]) -> dict | None:
    if not points:
        return None
    return min(points, key=lambda point: (main_score(point), str(point.get("code") or "")))


def as_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower() if value is not None else ""
    if text in {"true", "1", "on", "yes"}:
        return True
    if text in {"false", "0", "off", "no"}:
        return False
    return None


def live_values(device: dict) -> dict[str, object]:
    """DP -> value from the last successful local read."""
    values = device.get("live_values")
    return values if isinstance(values, dict) else {}


def point_value(device: dict, point: dict):
    dp = str(point.get("dp") or "")
    live = live_values(device)
    if dp in live:
        return live[dp]
    return point.get("cloud_value")
