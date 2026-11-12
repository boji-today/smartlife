import json
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import websocket

from smartlife.activity_intervals import rebuild_occupied
from smartlife.activity_sync import fetch_history, sync_one
from smartlife.db import connect, ensure_columns, insert_reading
from smartlife.log import log_error
from smartlife.ws import split_page

HA_WS = (
    "config/device_registry/list",
    "config/entity_registry/list",
    "get_states",
)
DEFAULT_DOMAINS = ("sensor", "humidifier", "climate")
RULES = (
    {
        "domain": "sensor",
        "device_class": "temperature",
        "column": "temp_c",
        "units": ("°C",),
        "priority": 0,
    },
    {
        "domain": "sensor",
        "device_class": "humidity",
        "column": "humidity",
        "units": ("%",),
        "priority": 0,
    },
    {
        "domain": "sensor",
        "device_class": "battery",
        "column": "battery",
        "units": ("%",),
        "priority": 0,
    },
    {
        "domain": "sensor",
        "device_class": "carbon_dioxide",
        "column": "co2_ppm",
        "units": ("ppm",),
        "priority": 0,
    },
    {
        "domain": "sensor",
        "device_class": "pm25",
        "column": "pm25",
        "units": ("µg/m³", "ug/m³", "μg/m³"),
        "priority": 0,
    },
    {
        "domain": "humidifier",
        "attr": "current_humidity",
        "column": "humidity",
        "units": ("%",),
        "priority": 1,
    },
    {
        "domain": "climate",
        "attr": "current_temperature",
        "column": "temp_c",
        "units": ("°C",),
        "priority": 1,
    },
    {
        "domain": "binary_sensor",
        "device_class": "door",
        "column": "switch",
        "states": {"on": 1, "off": 0},
        "priority": 0,
    },
)
SHOW = ("co2_ppm", "temp_c", "humidity", "battery", "pm25", "switch")
_logged = set()


def ha_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if not text or text in {"unavailable", "unknown", "none", "None"}:
            return None
        number = float(text)
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    if number.is_integer():
        return int(number)
    return number


def parse_ha_domains(raw):
    if raw is None:
        return DEFAULT_DOMAINS, None
    if isinstance(raw, str):
        if not raw.strip():
            return None, "invalid ha_domains"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None, "invalid ha_domains"
    elif isinstance(raw, list):
        data = raw
    else:
        return None, "invalid ha_domains"
    if not isinstance(data, list) or any(not isinstance(item, str) for item in data):
        return None, "invalid ha_domains"
    return tuple(data), None


def ha_entity_domain(entity_id):
    if not isinstance(entity_id, str) or "." not in entity_id:
        return ""
    return entity_id.split(".", 1)[0]


def entity_device_class(entry, state):
    attrs = state.get("attributes") if isinstance(state, dict) else None
    if isinstance(attrs, dict) and attrs.get("device_class"):
        return attrs.get("device_class")
    return entry.get("original_device_class") or ""


def rules_for(domain, device_class):
    found = []
    for rule in RULES:
        if rule["domain"] != domain:
            continue
        if "device_class" in rule:
            if rule["device_class"] == device_class:
                found.append(rule)
        else:
            found.append(rule)
    return found


def metric_value(state, rule):
    attrs = state.get("attributes") if isinstance(state, dict) else None
    if not isinstance(attrs, dict):
        attrs = {}
    if "attr" in rule:
        if rule["attr"] not in attrs:
            return None, f"missing {rule['attr']}"
        raw = attrs.get(rule["attr"])
    else:
        if not isinstance(state, dict) or "state" not in state:
            return None, "missing state"
        raw = state.get("state")
    if "states" in rule:
        if raw is None:
            return None, "null"
        if not isinstance(raw, str):
            return None, "not on/off"
        token = raw.strip()
        if token in {"unknown", "unavailable"}:
            return None, token
        if token not in rule["states"]:
            return None, "not on/off"
        return rule["states"][token], None
    units = []
    for key in ("unit_of_measurement", "temperature_unit"):
        if key == "temperature_unit" and rule["column"] != "temp_c":
            continue
        unit = attrs.get(key)
        if unit is not None and str(unit).strip() != "":
            units.append(str(unit).strip())
    for unit_text in units:
        if unit_text not in rule["units"]:
            return None, f"unit {unit_text}"
    if raw is None:
        return None, "null"
    if isinstance(raw, str):
        token = raw.strip()
        if token in {"unknown", "unavailable"}:
            return None, token
    try:
        number = ha_number(raw)
    except ValueError:
        return None, "not numeric"
    if number is None:
        return None, "not numeric"
    return number, None


def select_entities(entries, device_id, domains):
    found = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("device_id") != device_id:
            continue
        if entry.get("disabled_by"):
            continue
        entity_id = entry.get("entity_id") or ""
        if ha_entity_domain(entity_id) not in domains:
            continue
        found.append(entry)
    return found


def map_ha(entries, state_by_id):
    raw = []
    buckets = {}
    pending = []
    warnings = []
    for entry in entries:
        entity_id = entry.get("entity_id") or ""
        try:
            state = state_by_id.get(entity_id)
            if isinstance(state, dict):
                raw.append(state)
            device_class = entity_device_class(entry, state)
            rules = rules_for(ha_entity_domain(entity_id), device_class)
            if not isinstance(state, dict):
                if rules:
                    warnings.append((entity_id, "no state"))
                else:
                    pending.append((entity_id, "no state"))
                continue
            if not rules:
                pending.append((entity_id, "no mapping rule"))
                continue
            for rule in rules:
                value, reason = metric_value(state, rule)
                buckets.setdefault((rule["column"], rule["priority"]), []).append(
                    {"entity_id": entity_id, "value": value, "reason": reason}
                )
        except Exception as exc:
            warnings.append((entity_id, str(exc)))
    chosen = {}
    columns = sorted({column for column, _priority in buckets})
    for column in columns:
        priorities = sorted({priority for col, priority in buckets if col == column})
        for priority in priorities:
            group = buckets[(column, priority)]
            usable = [item for item in group if item["value"] is not None]
            for item in group:
                if item["reason"]:
                    warnings.append((item["entity_id"], item["reason"]))
            if len(usable) > 1:
                reason = f"{column} count {len(usable)}"
                for item in usable:
                    warnings.append((item["entity_id"], reason))
                break
            if len(usable) == 1:
                chosen[column] = usable[0]["value"]
                break
    if not chosen:
        warnings.extend(pending)
    return chosen, raw, warnings


def assess_ha(row, device_ids, entries, state_by_id):
    domains, error = parse_ha_domains(row.get("ha_domains"))
    empty = {"action": "skip", "reason": None, "values": {}, "raw": [], "warnings": []}
    if error:
        empty["reason"] = error
        return empty
    if not domains:
        empty["reason"] = "ha_domains empty"
        return empty
    if row["biz_id"] not in device_ids:
        empty["reason"] = "device not in registry"
        return empty
    selected = select_entities(entries, row["biz_id"], domains)
    if not selected:
        empty["reason"] = "no entity"
        return empty
    values, raw, warnings = map_ha(selected, state_by_id)
    return {
        "action": "insert",
        "reason": None,
        "values": values,
        "raw": raw,
        "warnings": warnings,
    }


def log_ha_warn(biz_id, entity_id, reason):
    key = (biz_id, entity_id, reason)
    if key in _logged:
        return
    _logged.add(key)
    log_error(f"{biz_id} {entity_id} {reason}")


def apply_ha(row, device_ids, entries, state_by_id, ts):
    result = assess_ha(row, device_ids, entries, state_by_id)
    biz_id = row.get("biz_id", "")
    for entity_id, reason in result["warnings"]:
        log_ha_warn(biz_id, entity_id, reason)
    if result["action"] != "insert":
        log_error(f"{biz_id} {result['reason']}")
        return result
    insert_reading(row, ts, result["values"], result["raw"])
    parts = [f"{column}={result['values'][column]}" for column in SHOW if column in result["values"]]
    if parts:
        print(f"{ts} {biz_id} {' '.join(parts)}", flush=True)
    return result


def collect_snapshot(rows, devices, entries, states, ts):
    device_ids = set()
    if isinstance(devices, list):
        for device in devices:
            if isinstance(device, dict) and device.get("id"):
                device_ids.add(device["id"])
    state_by_id = {}
    if isinstance(states, list):
        for item in states:
            if isinstance(item, dict) and item.get("entity_id"):
                state_by_id[item["entity_id"]] = item
    if not isinstance(entries, list):
        entries = []
    for row in rows:
        try:
            apply_ha(row, device_ids, entries, state_by_id, ts)
        except Exception as exc:
            log_error(f"{row.get('biz_id', '')} {exc}")


def ha_commands(ws, token):
    hello = json.loads(ws.recv())
    if hello.get("type") != "auth_required":
        raise ConnectionError("unexpected websocket hello")
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    auth = json.loads(ws.recv())
    if auth.get("type") != "auth_ok":
        raise ConnectionError("websocket auth failed")
    jobs = []
    for idx, command in enumerate(HA_WS, start=1):
        jobs.append({"id": idx, "command": command, "items": [], "done": False})
        ws.send(json.dumps({"id": idx, "type": command}))
    by_id = {job["id"]: job for job in jobs}
    next_id = len(jobs) + 1
    while any(not job["done"] for job in jobs):
        msg = json.loads(ws.recv())
        job = by_id.get(msg.get("id"))
        if job is None:
            continue
        if not msg.get("success", False):
            err = msg.get("error") or {}
            raise ConnectionError(err.get("message") or job["command"])
        chunk, cursor = split_page(msg.get("result"))
        job["items"].extend(chunk)
        if not cursor:
            job["done"] = True
            continue
        follow = {"id": next_id, "type": job["command"], "page": cursor}
        by_id[next_id] = job
        next_id += 1
        ws.send(json.dumps(follow))
    return {job["command"]: job["items"] for job in jobs}


def ha_query(url, token):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConnectionError("bad HA_URL")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    ws_url = f"{scheme}://{host}:{port}/api/websocket"
    # 1.9.2 只有带上 http_proxy_host 才会留下 http_no_proxy，* 使该地址不会被使用
    ws = websocket.create_connection(
        ws_url,
        timeout=60,
        suppress_origin=True,
        http_proxy_host="127.0.0.1",
        http_proxy_port=9,
        http_no_proxy=["*"],
    )
    try:
        return ha_commands(ws, token)
    finally:
        ws.close()


def ha_snapshot(url, token):
    error = None
    for _ in range(2):
        try:
            return ha_query(url, token)
        except Exception as exc:
            error = exc
    raise error


def collect_activity(rows, entries, url, token, now=None):
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(entries, list):
        log_error("ha activity registry missing")
        return
    conn = connect()
    try:
        ensure_columns(conn)
        for row in rows:
            biz_id = row.get("biz_id", "")
            try:
                status = sync_one(
                    conn,
                    row,
                    entries,
                    now,
                    lambda entity_id, start, end: fetch_history(url, token, entity_id, start, end),
                )
            except Exception as exc:
                log_error(f"ha activity {biz_id} {exc}")
                continue
            if status != "ok":
                continue
            try:
                rebuild_occupied(conn, row["id"])
            except Exception as exc:
                log_error(f"ha activity intervals {biz_id} {exc}")
    finally:
        conn.close()


def collect_ha(rows):
    try:
        url = os.environ.get("HA_URL", "").rstrip("/")
        token = os.environ.get("HA_TOKEN", "")
        if not url or not token:
            log_error("HA_URL or HA_TOKEN missing")
            return
        found = ha_snapshot(url, token)
        if any(name not in found for name in HA_WS):
            raise ConnectionError("incomplete ha snapshot")
    except Exception as exc:
        log_error(f"ha {exc}")
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        collect_snapshot(
            rows,
            found["config/device_registry/list"],
            found["config/entity_registry/list"],
            found["get_states"],
            ts,
        )
    except Exception as exc:
        log_error(f"ha {exc}")
    try:
        collect_activity(rows, found["config/entity_registry/list"], url, token)
    except Exception as exc:
        log_error(f"ha {exc}")
