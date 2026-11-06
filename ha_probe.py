#!/usr/bin/env python3
"""Read-only Home Assistant probe. GET states and list registries only."""

import base64
import json
import os
import socket
import sqlite3
import struct
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_WS = (
    "config/area_registry/list",
    "config/floor_registry/list",
    "config/entity_registry/list",
    "config/device_registry/list",
)

def load_env(path=".env"):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    url = os.environ.get("HA_URL", "").rstrip("/")
    token = os.environ.get("HA_TOKEN", "")
    if not url or not token:
        raise SystemExit("HA_URL or HA_TOKEN missing")
    return url, token


def http_get(url, token, path):
    req = urllib.request.Request(
        url + path,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as resp:
        return json.load(resp)


def _send_frame(sock, opcode, payload):
    mask = os.urandom(4)
    n = len(payload)
    header = bytearray([0x80 | opcode])
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", n))
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("websocket closed")
        buf.extend(chunk)
    return bytes(buf)


def _recv_message(sock):
    parts = []
    while True:
        b0, b1 = _recv_exact(sock, 2)
        fin = b0 & 0x80
        op = b0 & 0x0F
        length = b1 & 0x7F
        if b1 & 0x80:
            raise ConnectionError("server frame is masked")
        if length == 126:
            length = struct.unpack("!H", _recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
        data = _recv_exact(sock, length) if length else b""
        if op == 0x8:
            return None
        if op == 0x9:
            _send_frame(sock, 0xA, data)
            continue
        if op == 0xA:
            continue
        if op in (0x1, 0x0):
            parts.append(data)
        if fin and parts:
            return b"".join(parts).decode()


def _read_http_headers(sock):
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("websocket handshake closed")
        buf.extend(chunk)
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    return head.decode(), rest


def ws_lists(url, token):
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    sock = socket.create_connection((host, port), timeout=60)
    sock.settimeout(60)
    key = base64.b64encode(os.urandom(16)).decode()
    host_header = f"{host}:{port}"
    sock.sendall(
        (
            "GET /api/websocket HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode()
    )
    status, leftover = _read_http_headers(sock)
    if " 101 " not in status.split("\r\n", 1)[0]:
        raise ConnectionError("websocket upgrade failed")

    pending = bytearray(leftover)

    def recv_exact(n):
        while len(pending) < n:
            chunk = sock.recv(max(4096, n - len(pending)))
            if not chunk:
                raise ConnectionError("websocket closed")
            pending.extend(chunk)
        out = bytes(pending[:n])
        del pending[:n]
        return out

    def recv_message():
        parts = []
        while True:
            b0, b1 = recv_exact(2)
            fin = b0 & 0x80
            op = b0 & 0x0F
            length = b1 & 0x7F
            if b1 & 0x80:
                raise ConnectionError("server frame is masked")
            if length == 126:
                length = struct.unpack("!H", recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", recv_exact(8))[0]
            data = recv_exact(length) if length else b""
            if op == 0x8:
                return None
            if op == 0x9:
                _send_frame(sock, 0xA, data)
                continue
            if op == 0xA:
                continue
            if op in (0x1, 0x0):
                parts.append(data)
            if fin and parts:
                return b"".join(parts).decode()

    # replace module-level helpers for this connection's leftover buffer
    global _recv_exact, _recv_message
    _recv_exact = recv_exact
    _recv_message = recv_message

    hello = json.loads(recv_message())
    version = hello.get("ha_version", "")
    if hello.get("type") != "auth_required":
        raise ConnectionError("unexpected websocket hello")
    _send_frame(sock, 0x1, json.dumps({"type": "auth", "access_token": token}).encode())
    auth = json.loads(recv_message())
    if auth.get("type") != "auth_ok":
        raise ConnectionError("websocket auth failed")
    version = auth.get("ha_version") or version

    results = {}
    for idx, command in enumerate(ALLOWED_WS, start=1):
        _send_frame(
            sock,
            0x1,
            json.dumps({"id": idx, "type": command}).encode(),
        )
    while len(results) < len(ALLOWED_WS):
        raw = recv_message()
        if raw is None:
            break
        msg = json.loads(raw)
        if "id" not in msg:
            continue
        if not msg.get("success", False):
            err = msg.get("error") or {}
            results[msg["id"]] = {"error": err.get("message", "failed")}
        else:
            results[msg["id"]] = msg.get("result")
    sock.close()
    named = {}
    for idx, command in enumerate(ALLOWED_WS, start=1):
        named[command] = results.get(idx)
    named["ha_version"] = version
    return named


def load_ha_devices():
    db = Path(__file__).resolve().parent / "data" / "smartlife.db"
    if not db.exists():
        raise SystemExit("smartlife.db not found")
    uri = f"file:{db}?immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            """
            SELECT biz_id, name
            FROM devices
            WHERE source = 'ha'
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise SystemExit("ha device not found")
    return rows


def area_name(entry, devices, areas):
    area_id = entry.get("area_id")
    if not area_id:
        device = devices.get(entry.get("device_id") or "")
        if device:
            area_id = device.get("area_id")
    return areas.get(area_id or "", "")


def main():
    url, token = load_env()
    try:
        http_get(url, token, "/api/")
        states = http_get(url, token, "/api/states")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"GET failed: {exc.code}") from None

    registries = ws_lists(url, token)
    areas = {}
    area_rows = registries.get("config/area_registry/list") or []
    if isinstance(area_rows, dict):
        raise SystemExit(f"areas: {area_rows.get('error', 'failed')}")
    for area in area_rows:
        areas[area.get("area_id")] = area.get("name") or area.get("area_id")

    devices = {}
    device_rows = registries.get("config/device_registry/list") or []
    if isinstance(device_rows, list):
        for device in device_rows:
            devices[device.get("id")] = device

    entities = registries.get("config/entity_registry/list") or []
    if isinstance(entities, dict):
        raise SystemExit(f"entities: {entities.get('error', 'failed')}")
    by_device = {}
    for entry in entities:
        by_device.setdefault(entry.get("device_id"), []).append(entry)
    state_by_id = {row.get("entity_id"): row for row in states}

    print(f"ha {registries.get('ha_version') or '?'}")
    for biz_id, name in load_ha_devices():
        print(f"{name}\t{biz_id}")
        lines = []
        for entry in by_device.get(biz_id) or []:
            entity_id = entry.get("entity_id") or ""
            state = state_by_id.get(entity_id) or {}
            attrs = state.get("attributes") or {}
            kind = attrs.get("device_class") or entity_id.split(".", 1)[0]
            unit = attrs.get("unit_of_measurement") or ""
            value = state.get("state")
            shown = f"{value} {unit}".strip() if value is not None else ""
            lines.append(
                (
                    area_name(entry, devices, areas),
                    kind,
                    attrs.get("friendly_name") or entity_id,
                    shown,
                )
            )
        lines.sort()
        for room, kind, friendly, shown in lines:
            print(f"  {room or '-'}\t{kind}\t{friendly}\t{shown}")


if __name__ == "__main__":
    main()
