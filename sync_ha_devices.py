#!/usr/bin/env python3
"""Write HA rooms onto devices and insert the door sensor. HA calls are read-only."""

import os
from datetime import datetime, timezone
from pathlib import Path

import ha_probe
from smartlife import ha
from smartlife.db import connect, ensure_columns, utc_text
from smartlife.paths import configure, load_dotenv

DOOR_NAME = "门窗传感器"
DOORBELL_NAME = "小米智能门铃3"
DOOR_DOMAINS = '["sensor","binary_sensor"]'


def device_label(device):
    return (device.get("name_by_user") or device.get("name") or "").strip()


def room_of(device, entries, areas):
    area_id = device.get("area_id")
    if not area_id:
        for entry in entries:
            if isinstance(entry, dict) and entry.get("area_id"):
                area_id = entry.get("area_id")
                break
    if not area_id:
        return ""
    return areas.get(area_id) or ""


def battery_number(entries, state_by_id):
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("disabled_by"):
            continue
        entity_id = entry.get("entity_id") or ""
        if ha.ha_entity_domain(entity_id) != "sensor":
            continue
        state = state_by_id.get(entity_id)
        device_class = ha.entity_device_class(entry, state if isinstance(state, dict) else None)
        for rule in ha.rules_for("sensor", device_class):
            if rule.get("column") != "battery" or not isinstance(state, dict):
                continue
            value, _reason = ha.metric_value(state, rule)
            if value is not None:
                return value
    return None


def _one_named(devices, name):
    found = [device for device in devices.values() if isinstance(device, dict) and device_label(device) == name]
    if len(found) != 1 or not found[0].get("id"):
        raise SystemExit(f"{name} not found")
    return found[0]


def sync(conn, devices, entries_by_device, areas, state_by_id, updated_at):
    rows = list(conn.execute("SELECT * FROM devices WHERE source = 'ha' ORDER BY id"))
    by_biz = {row["biz_id"]: row for row in rows}
    door = _one_named(devices, DOOR_NAME)
    door_id = door["id"]
    door_entries = entries_by_device.get(door_id) or []
    doorbell_row = next((row for row in rows if row["name"] == DOORBELL_NAME), None)
    if doorbell_row is None:
        raise SystemExit("doorbell not in devices")
    bell_entries = entries_by_device.get(doorbell_row["biz_id"]) or []
    door_battery = battery_number(door_entries, state_by_id)
    bell_battery = battery_number(bell_entries, state_by_id)
    if door_battery is None:
        raise SystemExit("door sensor has no battery")
    if bell_battery is None:
        raise SystemExit("doorbell has no battery")

    planned = []
    for row in rows:
        device = devices.get(row["biz_id"])
        if not isinstance(device, dict):
            raise SystemExit(f"missing registry {row['name']}")
        entries = entries_by_device.get(row["biz_id"]) or []
        room = room_of(device, entries, areas)
        if not room:
            raise SystemExit(f"missing room {row['name']}")
        label = None
        if row["category"] == "pir1":
            label = device_label(device)
            if not label:
                raise SystemExit(f"missing name {row['name']}")
        planned.append((row, room, label))

    door_room = room_of(door, door_entries, areas)
    door_label = device_label(door)
    if not door_room or door_label != DOOR_NAME:
        raise SystemExit("door sensor fields")

    for row, room, label in planned:
        if label is None:
            conn.execute("UPDATE devices SET room = ? WHERE id = ?", (room, row["id"]))
        else:
            conn.execute(
                "UPDATE devices SET name = ?, biz_name = ?, room = ? WHERE id = ?",
                (label, label, room, row["id"]),
            )
    inserted = door_id not in by_biz
    if inserted:
        conn.execute(
            """
            INSERT INTO devices (
              biz_id, biz_name, name, source, room, active,
              ha_domains, ha_event_selectors, updated_at
            ) VALUES (?, ?, ?, 'ha', ?, 1, ?, '[]', ?)
            """,
            (door_id, door_label, door_label, door_room, DOOR_DOMAINS, updated_at),
        )
    else:
        conn.execute(
            "UPDATE devices SET ha_domains = ? WHERE source = 'ha' AND biz_id = ?",
            (DOOR_DOMAINS, door_id),
        )

    bell = conn.execute("SELECT * FROM devices WHERE id = ?", (doorbell_row["id"],)).fetchone()
    stored = conn.execute(
        "SELECT * FROM devices WHERE source = 'ha' AND biz_id = ?",
        (door_id,),
    ).fetchone()
    if bell["ha_domains"] is not None or bell["active"] != 1:
        raise SystemExit("doorbell will not collect battery")
    if stored is None or stored["active"] != 1 or stored["ha_domains"] != DOOR_DOMAINS:
        raise SystemExit("door sensor will not collect battery")
    if stored["name"] != DOOR_NAME or not stored["room"]:
        raise SystemExit("door sensor fields")
    listed = conn.execute(
        "SELECT name, room FROM devices WHERE source = 'ha' ORDER BY id"
    ).fetchall()
    return {
        "inserted": inserted,
        "doorbell_battery": bell_battery,
        "door_battery": door_battery,
        "rows": [(row["name"], row["room"]) for row in listed],
    }


def load_ha():
    url = os.environ.get("HA_URL", "").rstrip("/")
    token = os.environ.get("HA_TOKEN", "")
    if not url or not token:
        raise SystemExit("HA_URL or HA_TOKEN missing")
    try:
        ha_probe.http_get(url, token, "/api/")
        states = ha_probe.http_get(url, token, "/api/states")
    except Exception as exc:
        raise SystemExit(f"GET failed: {exc}") from None
    registries = ha_probe.ws_lists(url, token)
    areas = {}
    area_rows = registries.get("config/area_registry/list") or []
    if isinstance(area_rows, dict):
        raise SystemExit("areas failed")
    for area in area_rows:
        if isinstance(area, dict) and area.get("area_id"):
            areas[area["area_id"]] = area.get("name") or area["area_id"]
    devices = {}
    device_rows = registries.get("config/device_registry/list") or []
    if not isinstance(device_rows, list):
        raise SystemExit("devices failed")
    for device in device_rows:
        if isinstance(device, dict) and device.get("id"):
            devices[device["id"]] = device
    entities = registries.get("config/entity_registry/list") or []
    if isinstance(entities, dict):
        raise SystemExit("entities failed")
    entries_by_device = {}
    for entry in entities:
        if isinstance(entry, dict):
            entries_by_device.setdefault(entry.get("device_id"), []).append(entry)
    state_by_id = {}
    if isinstance(states, list):
        for item in states:
            if isinstance(item, dict) and item.get("entity_id"):
                state_by_id[item["entity_id"]] = item
    return devices, entries_by_device, areas, state_by_id


def main():
    configure(Path(__file__).resolve().parent)
    load_dotenv()
    devices, entries_by_device, areas, state_by_id = load_ha()
    updated_at = utc_text(datetime.now(timezone.utc))
    conn = connect()
    try:
        ensure_columns(conn)
        try:
            result = sync(conn, devices, entries_by_device, areas, state_by_id, updated_at)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    for name, room in result["rows"]:
        print(f"{name}\t{room}", flush=True)
    action = "insert" if result["inserted"] else "keep"
    print(f"{action} {DOOR_NAME} battery={result['door_battery']}", flush=True)
    print(f"doorbell battery={result['doorbell_battery']}", flush=True)


if __name__ == "__main__":
    main()
