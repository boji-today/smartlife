import json
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import websocket

from smartlife.db import insert_reading
from smartlife.log import log_error
from smartlife.ws import split_page

HA_WS = (
    "config/entity_registry/list",
    "get_states",
)
HA_CLASS_COLUMNS = {
    "temperature": "temp_c",
    "humidity": "humidity",
}


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
    return jobs[0]["items"], jobs[1]["items"]


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


def store_ha_reading(row, entity_ids, state_by_id, ts):
    biz_id = row["biz_id"]
    grouped = {kind: [] for kind in HA_CLASS_COLUMNS}
    for entity_id in entity_ids:
        state = state_by_id.get(entity_id)
        if not isinstance(state, dict):
            continue
        kind = (state.get("attributes") or {}).get("device_class")
        if kind in grouped:
            grouped[kind].append(state)
    chosen = {}
    raw = []
    for kind, column in HA_CLASS_COLUMNS.items():
        states = grouped[kind]
        if kind == "humidity" and not states:
            continue
        if len(states) != 1:
            raise RuntimeError(f"{kind} count {len(states)}")
        try:
            number = ha_number(states[0].get("state"))
        except ValueError:
            number = None
        if number is None:
            raise RuntimeError(f"{kind} not numeric")
        chosen[column] = number
        raw.append(states[0])
    if "temp_c" not in chosen:
        return
    insert_reading(row, ts, chosen, raw)
    humidity = chosen.get("humidity")
    suffix = "" if humidity is None else f" humidity={humidity}"
    print(f"{ts} {biz_id} temp_c={chosen['temp_c']}{suffix}", flush=True)


def collect_ha(rows):
    try:
        url = os.environ.get("HA_URL", "").rstrip("/")
        token = os.environ.get("HA_TOKEN", "")
        if not url or not token:
            log_error("HA_URL or HA_TOKEN missing")
            return
        entities, states = ha_snapshot(url, token)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state_by_id = {
            item.get("entity_id"): item
            for item in states
            if isinstance(item, dict) and item.get("entity_id")
        }
        sensors = {}
        for entry in entities:
            if not isinstance(entry, dict):
                continue
            entity_id = entry.get("entity_id") or ""
            device_id = entry.get("device_id")
            if not device_id or not entity_id.startswith("sensor."):
                continue
            sensors.setdefault(device_id, []).append(entity_id)
        for row in rows:
            try:
                store_ha_reading(row, sensors.get(row["biz_id"], []), state_by_id, ts)
            except Exception as exc:
                log_error(f"{row['biz_id']} {exc}")
    except Exception as exc:
        log_error(f"ha {exc}")
