"""Local-only device control, served at /local by the existing web app.

Uses the encrypted device cache (keys) and LAN scan cache (IP + protocol) from
Tuya Local Key. No cloud request is made here: status reads and commands go
straight to the device over tinytuya.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import time
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

import device_cache
import device_values
import lan_scan

bp = Blueprint("local_control", __name__)

SESSION_FILE = os.environ.get(
    "SESSION_FILE", os.path.expanduser("~/.config/tuya-smartlife/session.json")
)
_SESSION_DIR = os.path.dirname(SESSION_FILE) or "."
DEVICE_CACHE_FILE = os.environ.get(
    "DEVICE_CACHE_FILE", os.path.join(_SESSION_DIR, "devices.cache")
)
DEVICE_CACHE_KEY_FILE = os.environ.get(
    "DEVICE_CACHE_KEY_FILE", os.path.join(_SESSION_DIR, "cache.key")
)
LAN_CACHE_FILE = os.environ.get("LAN_CACHE_FILE", os.path.join(_SESSION_DIR, "lan.cache"))
# Plain, non-secret memory of the last LAN address that worked. This lets the
# app reconnect on the next launch without a full scan. No keys are stored here.
LAN_PROFILE_FILE = os.environ.get(
    "LAN_PROFILE_FILE", os.path.join(_SESSION_DIR, "lan-profile.json")
)

MAX_VALUE_BYTES = 4096
# A failed read should not start a subnet scan on every refresh. Back off from
# 2min to 30min, and never scan when the saved address still answers on 6668.
RECONNECT_MIN_SECONDS = 120
RECONNECT_MAX_SECONDS = 1800
TCP_PROBE_SECONDS = 0.4

_ensure_lock = threading.Lock()
_ensure_state = {
    "running": False, "started_at": 0.0, "finished_at": 0.0,
    "swept": 0, "addresses": 0, "matched": 0, "reason": "",
}
MAX_STRING_CHARS = 256

# The scan cache is the source of truth for the IP. These messages only explain
# why it could not be resolved, without exposing keys or raw SDK errors.
_ISSUE_MESSAGES = {
    "missing_ip": "IP abhi save nahi hua. Auto scan apne aap chal raha hai — thoda wait karein.",
    "missing_protocol": "Protocol version nahi mila. Scan network dobara chalayein.",
    "no_local_key": "Local key nahi mili. Main screen par dobara login karein.",
    "gateway_unavailable": "Is sub-device ka gateway cache me nahi. Refresh karein phir Scan network.",
    "missing_node_id": "Sub-device node ID nahi mila. Dobara login karein.",
    "unknown_device": "Ye device list me nahi. Refresh karein.",
}


def _message(code: str) -> str:
    return _ISSUE_MESSAGES.get(code, code)


def _ip_hint(device: dict) -> str:
    """A cached cloud IP, used only as a scan hint / one-device check target."""
    text = str(device.get("ip") or "").strip()
    try:
        if text and ipaddress.ip_address(text):
            return text
    except ValueError:
        pass
    return ""


def _normalise_hint(text: str) -> str:
    """A detected IP or subnet, normalised to a scan-ready /24 network."""
    text = str(text or "").strip()
    if not text:
        return ""
    try:
        if "/" in text:
            network = ipaddress.ip_network(text, strict=False)
        else:
            network = ipaddress.ip_network(f"{ipaddress.ip_address(text)}/24", strict=False)
    except ValueError:
        return ""
    return str(network) if network.version == 4 else ""


def _detected_subnet() -> str:
    """The subnet Android/Windows detected, or the configured LAN_SUBNET."""
    for name in ("TUYA_NETWORK_HINT", "LAN_SUBNET"):
        value = _normalise_hint(os.environ.get(name, ""))
        if value:
            return value
    return ""


def _subnet_hint(devices: list[dict]) -> str:
    detected = _detected_subnet()
    if detected:
        return detected
    for device in devices:
        ip = _ip_hint(device)
        if ip:
            return str(ipaddress.ip_network(f"{ip}/24", strict=False))
    return ""


def _body(path: str) -> dict:
    entry = device_cache.load(path, DEVICE_CACHE_KEY_FILE)
    return (entry or {}).get("body") or {}


def _devices() -> list[dict]:
    rows = _body(DEVICE_CACHE_FILE).get("devices") or []
    return [row for row in rows if isinstance(row, dict)]


def _results() -> dict:
    rows = _body(LAN_CACHE_FILE).get("results") or {}
    return rows if isinstance(rows, dict) else {}


def _profiles() -> dict:
    """Read the saved LAN connections (IP/version only, never a local key)."""
    try:
        data = json.loads(Path(LAN_PROFILE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_profiles(profiles: dict) -> None:
    path = Path(LAN_PROFILE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(profiles, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


def _merged_results() -> dict:
    """LAN scan results plus the last addresses that actually answered.

    The scan cache belongs to the main app and can be cleared on a re-login;
    these profiles are local-control's own memory, so a working device keeps
    its address across launches.
    """
    results = dict(_results())
    for device_id, profile in _profiles().items():
        ip = str(profile.get("ip") or "").strip()
        version = str(profile.get("version") or "").strip()
        if not ip or version not in {"3.1", "3.2", "3.3", "3.4", "3.5"}:
            continue
        current = dict(results.get(device_id) or {})
        current_ts = float(current.get("checked_at") or current.get("last_ok") or 0)
        saved_ts = float(profile.get("last_ok") or profile.get("checked_at") or 0)
        if current.get("ip") and current_ts >= saved_ts:
            continue
        current.update({
            "status": lan_scan.OK,
            "ip": ip,
            "version": version,
            "checked_at": saved_ts or time.time(),
        })
        if "device22" in profile:
            current["device22"] = bool(profile["device22"])
        results[device_id] = current
    return results


def _remember_success(device_id: str, context: dict) -> None:
    profiles = _profiles()
    entry = dict(profiles.get(device_id) or {})
    now = time.time()
    entry.update({
        "ip": context["ip"],
        "version": str(context["version"]),
        "device22": bool(context.get("device22")),
        "subnet": _detected_subnet() or entry.get("subnet", ""),
        "last_ok": now,
        "checked_at": now,
        "fail_count": 0,
    })
    profiles[device_id] = entry
    _save_profiles(profiles)


def _remember_failure(device_id: str, context: dict, status: str) -> None:
    profiles = _profiles()
    entry = dict(profiles.get(device_id) or {})
    if context.get("ip"):
        entry.setdefault("ip", context["ip"])
        entry.setdefault("version", str(context["version"]))
    entry["fail_count"] = int(entry.get("fail_count") or 0) + 1
    entry["checked_at"] = time.time()
    profiles[device_id] = entry
    _save_profiles(profiles)
    if entry["fail_count"] >= 3 and status in {
        "unreachable", "no_values", "key_or_protocol", "connection_failed", "read_failed",
    }:
        _ensure_async()


def repair_state() -> dict:
    return dict(_ensure_state)


def _by_id(devices: list[dict]) -> dict[str, dict]:
    return {str(d.get("id")): d for d in devices if d.get("id")}


def _range(spec: dict) -> dict:
    """Read Tuya's `values` JSON string into min/max/step/options."""
    raw = spec.get("values")
    if not isinstance(raw, str):
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key in ("min", "max", "step"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
    options = data.get("range")
    if isinstance(options, list) and 1 <= len(options) <= 64:
        out["options"] = [str(item) for item in options]
    return out


def _points(device: dict) -> list[dict]:
    """DP metadata from the cached cloud spec, never the local key."""
    status = device.get("status") or {}
    functions = device.get("function") or {}
    ranges = device.get("status_range") or {}
    strategy = device.get("local_strategy") or {}
    points = []
    for dp, info in sorted(strategy.items(), key=lambda pair: int(pair[0]) if str(pair[0]).isdigit() else 9999):
        dp = str(dp)
        if not dp.isdigit():
            continue
        info = info if isinstance(info, dict) else {}
        code = str(info.get("status_code") or info.get("function_code") or dp)
        spec = functions.get(code) or ranges.get(code) or {}
        spec = spec if isinstance(spec, dict) else {}
        dtype = str(spec.get("type") or "").lower()
        if not dtype and code in status:
            value = status.get(code)
            dtype = "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "string"
        item = {"dp": dp, "code": code, "type": dtype}
        item.update(_range(spec))
        item["cloud_value"] = status.get(code)
        points.append(item)
    return points


def _public(device: dict, context: dict | None, issue: str = "") -> dict:
    return {
        "id": str(device.get("id") or ""),
        "name": str(device.get("name") or device.get("id") or ""),
        "category": str(device.get("category") or ""),
        "product_name": str(device.get("product_name") or ""),
        "ip": context["ip"] if context else "",
        "ip_hint": _ip_hint(device),
        "version": context["version"] if context else "",
        "sub": bool(context and context["node_id"]),
        "gateway": bool(context and context["gateway"]),
        "reachable": bool(context and context["ip"]),
        "issue": issue,
        "issue_message": _message(issue) if issue else "",
        "points": _points(device),
    }


def _context(device_id: str):
    devices = _devices()
    if not _by_id(devices).get(device_id):
        raise device_values.ReadUnavailable("unknown_device")
    return device_values.connection(devices, _merged_results(), device_id)


def _coerce(value, point: dict):
    """Validate a command against the cached DP spec. Returns (value, error)."""
    dtype = point.get("type", "")
    if dtype in ("bool", "boolean"):
        if isinstance(value, bool):
            return value, None
        text = str(value).strip().lower()
        if text in {"true", "1", "on", "yes"}:
            return True, None
        if text in {"false", "0", "off", "no"}:
            return False, None
        return None, "invalid_value"
    if dtype in ("int", "integer", "value"):
        try:
            number = int(str(value).strip())
        except (ValueError, TypeError):
            return None, "invalid_value"
        low, high = point.get("min"), point.get("max")
        if (low is not None and number < low) or (high is not None and number > high):
            return None, "out_of_range"
        return number, None
    if dtype in ("enum", "string", "str", "json", "raw"):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if len(text.encode("utf-8")) > MAX_VALUE_BYTES:
            return None, "value_too_large"
        if dtype == "enum":
            options = point.get("options") or []
            if options and text not in options:
                return None, "invalid_value"
            return text, None
        if dtype in ("json", "raw"):
            try:
                parsed = json.loads(text)
            except ValueError:
                return None, "invalid_value"
            if not isinstance(parsed, (dict, list)):
                return None, "invalid_value"
            return parsed, None
        if len(text) > MAX_STRING_CHARS:
            return None, "value_too_large"
        return text, None
    # Unknown type: only accept a small scalar, never an arbitrary object.
    if isinstance(value, (bool, int)):
        return value, None
    if isinstance(value, str) and len(value) <= MAX_STRING_CHARS:
        return value, None
    return None, "unsupported_dp_type"


def _send(context: dict, dp: str, value) -> dict:
    """Set one DP locally, closing every socket on all paths."""
    parent = child = None
    try:
        version = "3.3" if context["version"] == "3.2" else context["version"]
        parent = lan_scan._new_device(context["host_id"], context["ip"], context["key"], version)
        parent.disabledetect = context["gateway"]
        if context["device22"] or context["version"] == "3.2":
            parent.dev_type = "device22"
            parent.set_dpsUsed({item: None for item in context["dp_ids"]})
        device = parent
        if context["node_id"]:
            device = child = lan_scan._load_tinytuya().Device(
                context["device_id"], cid=context["node_id"], parent=parent,
                connection_timeout=lan_scan.PROBE_TIMEOUT_SECONDS,
                connection_retry_limit=1, connection_retry_delay=0,
            )
        reply = device.set_value(int(dp), value)
        if isinstance(reply, dict) and reply.get("Err"):
            return {"ok": False, "error": "device_error"}
        return {"ok": True}
    except OSError:
        return {"ok": False, "error": "unreachable"}
    except lan_scan.ScannerUnavailable:
        return {"ok": False, "error": "scanner_unavailable"}
    except Exception:
        return {"ok": False, "error": "command_failed"}
    finally:
        for device in (child, parent):
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass


# --- Silent LAN reconnect -------------------------------------------------
# Once a device has answered, its address is remembered. A later launch uses
# that address first and only does a short, throttled scan if the device no
# longer answers (for example after the phone joined a different Wi-Fi).

def _ensure_async() -> None:
    if _ensure_state["running"]:
        return
    threading.Thread(target=_ensure_connections, name="local-reconnect", daemon=True).start()


def _saved_address(device_id: str, results: dict, profiles: dict) -> tuple[str, str]:
    result = results.get(device_id) or {}
    profile = profiles.get(device_id) or {}
    ip = str(result.get("ip") or profile.get("ip") or "").strip()
    version = str(result.get("version") or profile.get("version") or "").strip()
    return ip, version


def _candidate_subnets(devices: list[dict], profiles: dict) -> list[str]:
    subnets = []
    detected = _detected_subnet()
    if detected:
        subnets.append(detected)
    for device in devices:
        hints = [profiles.get(str(device.get("id")) or {}, {}).get("subnet"), _ip_hint(device)]
        for hint in hints:
            normalised = _normalise_hint(hint)
            if normalised and normalised not in subnets:
                subnets.append(normalised)
        if len(subnets) >= 2:
            break
    return subnets[:2]


def _ensure_connections() -> None:
    with _ensure_lock:
        if _ensure_state["running"]:
            return
        _ensure_state.update({
            "running": True, "started_at": time.time(), "finished_at": 0.0,
            "swept": 0, "addresses": 0, "matched": 0, "reason": "checking",
        })
    try:
        devices = [d for d in _devices() if d.get("local_key") and d.get("id")]
        if not devices:
            _ensure_state["reason"] = "no_devices"
            return
        profiles = _profiles()
        results = _merged_results()
        missing = []
        for device in devices:
            try:
                device_values.connection(devices, results, str(device["id"]))
            except device_values.ReadUnavailable:
                missing.append(device)
        if not missing:
            _ensure_state["reason"] = "all_saved"
            return

        # A device that still answers on 6668 does not need a subnet scan: the
        # problem is its key/protocol, which scanning cannot fix.
        need_scan = []
        for device in missing:
            device_id = str(device["id"])
            ip, _ = _saved_address(device_id, results, profiles)
            if ip and lan_scan.tcp_state(ip, TCP_PROBE_SECONDS) == lan_scan.OPEN:
                continue
            need_scan.append(device)
        if not need_scan:
            _ensure_state["reason"] = "device_silent"
            return

        last_attempt = max(
            float((profiles.get(str(d["id"])) or {}).get("last_scan_at") or 0)
            for d in need_scan
        )
        worst_fails = max(
            int((profiles.get(str(d["id"])) or {}).get("fail_count") or 0)
            for d in need_scan
        )
        wait = min(RECONNECT_MAX_SECONDS, RECONNECT_MIN_SECONDS * (2 ** min(worst_fails, 5)))
        if time.time() - last_attempt < wait:
            _ensure_state["reason"] = "waiting"
            return

        subnets = _candidate_subnets(need_scan, profiles)
        if not subnets:
            _ensure_state["reason"] = "no_network"
            return

        targets = ",".join(subnets)
        try:
            addresses = lan_scan.parse_targets(targets)
        except lan_scan.TargetError:
            _ensure_state["reason"] = "bad_network"
            return

        now = time.time()
        for device in need_scan:
            device_id = str(device["id"])
            entry = dict(profiles.get(device_id) or {})
            entry["last_scan_at"] = now
            profiles[device_id] = entry
        _save_profiles(profiles)

        _ensure_state.update({"addresses": len(addresses), "swept": 0, "reason": "scanning"})
        observed = lan_scan.sweep(
            addresses, progress=lambda done: _ensure_state.__setitem__("swept", done))
        open_ips = [ip for ip, state in observed.items() if state == lan_scan.OPEN]

        for device in need_scan:
            device_id = str(device["id"])
            key = str(device.get("local_key") or "")
            saved_ip, saved_version = _saved_address(device_id, results, profiles)
            candidates = []
            for ip in [saved_ip] + open_ips:
                if ip and ip not in candidates:
                    candidates.append(ip)
            versions = []
            for version in (saved_version, device.get("version"), "3.4", "3.3", "3.2"):
                version = str(version or "").strip()
                if version in {"3.1", "3.2", "3.3", "3.4", "3.5"} and version not in versions:
                    versions.append(version)
            for ip in candidates:
                for version in versions:
                    try:
                        outcome = lan_scan.probe(device_id, ip, key, version)
                    except lan_scan.ScannerUnavailable:
                        _ensure_state["reason"] = "scanner_unavailable"
                        return
                    if not outcome.get("match"):
                        continue
                    results[device_id] = {
                        "status": lan_scan.OK, "ip": ip, "version": version,
                        "device22": bool(outcome.get("device22")), "checked_at": time.time(),
                    }
                    _remember_success(device_id, {
                        "ip": ip, "version": version, "device22": bool(outcome.get("device22")),
                    })
                    _ensure_state["matched"] += 1
                    break
                if results.get(device_id, {}).get("ip") == ip:
                    break
        _write_cache(LAN_CACHE_FILE, {
            "results": results,
            "summary": {"matched": len(results)},
            "targets": targets,
            "changes": None,
            "changes_at": None,
        })
        _ensure_state["reason"] = "done"
    except Exception:
        _ensure_state["reason"] = "failed"
    finally:
        _ensure_state["running"] = False
        _ensure_state["finished_at"] = time.time()


def _home_assistant() -> bool:
    return bool(
        os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
        or request.headers.get("X-Ingress-Path")
    )


def _tailscale_url() -> str:
    ip = os.environ.get("TAILSCALE_IP", "").strip()
    if not ip:
        return ""
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return ""
    port = os.environ.get("PORT", "8000").strip() or "8000"
    return f"http://{ip}:{port}/local"


@bp.get("/local")
def local_page():
    devices = _devices()
    ready = bool(devices) and any(d.get("local_key") for d in devices)
    return render_template(
        "local_control.html", ready=ready, device_count=len(devices),
        subnet_hint=_subnet_hint(devices), tailscale_url=_tailscale_url(),
        home_assistant=_home_assistant(),
    )


@bp.get("/api/local/devices")
def local_devices():
    devices = _devices()
    results = _merged_results()
    rows = []
    for device in devices:
        if not device.get("local_key"):
            continue
        issue = ""
        try:
            context = device_values.connection(devices, results, str(device.get("id")))
        except device_values.ReadUnavailable as error:
            context = None
            issue = str(error)
        rows.append(_public(device, context, issue))
    rows.sort(key=lambda row: row["name"].lower())
    return jsonify({
        "devices": rows,
        "scanned": any(
            (results.get(str(d.get("id"))) or {}).get("ip") for d in devices
        ),
        "setup_required": not bool(devices),
        "subnet_hint": _subnet_hint(devices),
        "repair": repair_state(),
    })


@bp.post("/api/local/ensure")
def local_ensure():
    """Silently use saved LAN addresses; scan only if they stopped answering."""
    if _ensure_state["running"]:
        return jsonify({"ok": True, **repair_state()})
    devices = [d for d in _devices() if d.get("local_key") and d.get("id")]
    results = _merged_results()
    missing = []
    for device in devices:
        try:
            device_values.connection(devices, results, str(device["id"]))
        except device_values.ReadUnavailable:
            missing.append(device)
    if missing and _detected_subnet():
        _ensure_async()
    return jsonify({"ok": True, **repair_state()})


@bp.get("/api/local/status")
def local_status():
    device_id = str(request.args.get("id") or "").strip()
    try:
        context = _context(device_id)
    except device_values.ReadUnavailable as error:
        code = str(error)
        return jsonify({"error": _message(code), "code": code}), 404
    outcome = device_values.read(context)
    if outcome.get("status") != "ok":
        _remember_failure(device_id, context, str(outcome.get("status") or "read_failed"))
        return jsonify(outcome)
    _remember_success(device_id, context)
    points = {item["dp"]: item for item in _points(_by_id(_devices())[device_id])}
    values = []
    for dp, value in sorted(outcome["dps"].items(), key=lambda pair: int(pair[0])):
        point = points.get(dp, {"dp": dp, "code": dp, "type": ""})
        point = dict(point, value=value)
        # Some devices have no cached DP spec. Infer the type from the live
        # read so a boolean switch still renders as a proper ON/OFF toggle.
        if not point.get("type"):
            if isinstance(value, bool):
                point["type"] = "boolean"
            elif isinstance(value, int):
                point["type"] = "integer"
            elif isinstance(value, str):
                point["type"] = "string"
        values.append(point)
    return jsonify({
        "status": "ok",
        "read_at": outcome.get("read_at"),
        "values": values,
    })


@bp.post("/api/local/command")
def local_command():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "invalid_request"}), 400
    device_id = str(data.get("device_id") or "").strip()
    dp = str(data.get("dp") or "").strip()
    if not device_id or not dp.isdigit():
        return jsonify({"error": "invalid_request"}), 400
    try:
        context = _context(device_id)
    except device_values.ReadUnavailable as error:
        code = str(error)
        return jsonify({"error": _message(code), "code": code}), 404
    points = {item["dp"]: item for item in _points(_by_id(_devices())[device_id])}
    point = points.get(dp)
    if not point or "value" not in data:
        return jsonify({"error": "unknown_dp"}), 400
    value, error = _coerce(data["value"], point)
    if error:
        return jsonify({"error": error}), 400
    outcome = _send(context, dp, value)
    if not outcome["ok"]:
        _remember_failure(device_id, context, outcome.get("error", "command_failed"))
        return jsonify(outcome), 502
    _remember_success(device_id, context)
    return jsonify({"ok": True, "dp": dp, "value": value})


# --- Manual devices -----------------------------------------------------
# A user with one phone cannot always scan the Smart Life QR. These endpoints
# let a device be added with its own local key and IP, with no cloud request.

def _write_cache(path: str, body: dict) -> None:
    existing = device_cache.load(path, DEVICE_CACHE_KEY_FILE) or {}
    device_cache.save(path, DEVICE_CACHE_KEY_FILE, {
        "session_key": existing.get("session_key") or "manual",
        "cached_at": time.time(),
        "body": body,
    })


@bp.post("/api/local/device")
def local_add_device():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "invalid_request"}), 400

    name = str(data.get("name") or "").strip()[:60]
    device_id = str(data.get("id") or "").strip()
    key = str(data.get("local_key") or "").strip()
    ip = str(data.get("ip") or "").strip()
    version = str(data.get("version") or "3.4").strip()
    dp = str(data.get("dp") or "1").strip()

    if not name or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", device_id):
        return jsonify({"error": "invalid_device_details"}), 400
    if not re.fullmatch(r"[A-Fa-f0-9]{16,32}", key):
        return jsonify({"error": "invalid_local_key"}), 400
    try:
        address = ipaddress.IPv4Address(ip)
    except ipaddress.AddressValueError:
        return jsonify({"error": "invalid_ip"}), 400
    if version not in {"3.1", "3.2", "3.3", "3.4"}:
        return jsonify({"error": "invalid_version"}), 400
    if not dp.isdigit() or not 1 <= int(dp) <= 255:
        return jsonify({"error": "invalid_dp"}), 400

    code = "switch_" + dp
    device = {
        "id": device_id,
        "name": name,
        "local_key": key,
        "ip": str(address),
        "online": True,
        "category": "manual",
        "product_name": "Manual Device",
        "status": {},
        "function": {code: {"code": code, "type": "Boolean", "values": "{}"}},
        "status_range": {},
        "local_strategy": {dp: {"status_code": code, "value_convert": "raw"}},
    }
    devices = [row for row in _devices() if str(row.get("id")) != device_id]
    devices.append(device)
    _write_cache(DEVICE_CACHE_FILE, {"devices": devices})

    results = _results()
    results[device_id] = {
        "status": "ok", "ip": str(address), "version": version,
        "device22": False, "checked_at": time.time(),
    }
    _write_cache(LAN_CACHE_FILE, {
        "results": results,
        "summary": {"matched": len(results)},
        "targets": str(ipaddress.ip_network(f"{address}/24", strict=False)),
        "changes": None, "changes_at": None,
    })
    profiles = _profiles()
    profiles[device_id] = {
        "ip": str(address), "version": version, "device22": False,
        "subnet": _detected_subnet() or str(ipaddress.ip_network(f"{address}/24", strict=False)),
        "last_ok": time.time(), "checked_at": time.time(), "fail_count": 0,
        "last_scan_at": 0,
    }
    _save_profiles(profiles)
    return jsonify({"ok": True, "id": device_id})


@bp.delete("/api/local/device")
def local_delete_device():
    device_id = str(request.args.get("id") or "").strip()
    if not device_id:
        return jsonify({"error": "invalid_request"}), 400
    devices = [row for row in _devices() if str(row.get("id")) != device_id]
    _write_cache(DEVICE_CACHE_FILE, {"devices": devices})
    results = _results()
    results.pop(device_id, None)
    _write_cache(LAN_CACHE_FILE, {"results": results, "summary": {"matched": len(results)},
                                  "targets": "", "changes": None, "changes_at": None})
    profiles = _profiles()
    profiles.pop(device_id, None)
    _save_profiles(profiles)
    return jsonify({"ok": True})
