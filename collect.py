import base64
import json
import os
import socket
import sqlite3
import ssl
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import tinytuya

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "smartlife.db"
LOG = ROOT / "collect.log"
POOL = ThreadPoolExecutor(max_workers=2)
READING_COLUMNS = (
    ("switch", "INTEGER"),
    ("pm25", "INTEGER"),
    ("filter_pct", "INTEGER"),
    ("air_quality", "TEXT"),
    ("fault", "INTEGER"),
)
METRIC_COLUMNS = (
    "co2_ppm",
    "co2_state",
    "temp_c",
    "humidity",
    "battery",
    "switch",
    "pm25",
    "filter_pct",
    "air_quality",
    "fault",
)
HA_WS = (
    "config/entity_registry/list",
    "get_states",
)
HA_CLASS_COLUMNS = {
    "temperature": "temp_c",
    "humidity": "humidity",
}


def load_dotenv(path=ROOT / ".env"):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def log_error(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {msg}\n")


def connect():
    conn = sqlite3.connect(DB, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL").fetchone()
    return conn


def ensure_columns(conn):
    for name, typ in READING_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE readings ADD COLUMN {name} {typ}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    conn.commit()


def metrics(category, dps):
    if category == "co2bj":
        return {
            "co2_ppm": dps.get("2"),
            "co2_state": dps.get("1"),
            "temp_c": dps.get("18"),
            "humidity": dps.get("19"),
            "battery": dps.get("15"),
        }
    if category == "kj":
        switch = dps.get("1")
        return {
            "switch": None if switch is None else int(bool(switch)),
            "pm25": dps.get("2"),
            "filter_pct": dps.get("5"),
            "air_quality": dps.get("21"),
            "fault": dps.get("22"),
        }
    return None


def collect_one(row):
    biz_id = row["biz_id"]
    try:
        result = read_status(biz_id, row["address"], row["version"], row["local_key"])
        if result is None:
            return
        dps = result.get("dps")
        if not dps:
            log_error(f"{biz_id} no dps")
            return
        values = metrics(row["category"], dps)
        if values is None:
            log_error(f"{biz_id} unknown category {row['category']}")
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        columns = ", ".join(METRIC_COLUMNS)
        placeholders = ", ".join("?" for _ in METRIC_COLUMNS)
        conn = connect()
        try:
            ensure_columns(conn)
            conn.execute(
                f"""
                INSERT INTO readings (
                  device_id, source, biz_id, ts, {columns}, raw_json
                ) VALUES (?, ?, ?, ?, {placeholders}, ?)
                """,
                (
                    row["id"],
                    row["source"],
                    biz_id,
                    ts,
                    *[values.get(name) for name in METRIC_COLUMNS],
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        if row["category"] == "kj":
            print(
                f"{ts} {biz_id} pm25={values.get('pm25')} air_quality={values.get('air_quality')}",
                flush=True,
            )
        else:
            print(f"{ts} {biz_id} co2_ppm={values.get('co2_ppm')}", flush=True)
    except Exception as exc:
        log_error(f"{biz_id} {exc}")


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


def split_page(result):
    if isinstance(result, list):
        return result, None
    if isinstance(result, dict):
        chunk = None
        for key in ("items", "entities", "states"):
            if isinstance(result.get(key), list):
                chunk = result[key]
                break
        if chunk is None:
            raise ConnectionError("unexpected ha page")
        cursor = result.get("next") or result.get("next_page") or result.get("cursor")
        if cursor in ("", None, 0, False):
            return chunk, None
        return chunk, cursor
    raise ConnectionError("unexpected ha result")


def _ws_send(sock, opcode, payload):
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


def _ws_handshake(sock, host, port):
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            "GET /api/websocket HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode()
    )
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("websocket handshake closed")
        buf.extend(chunk)
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    status = head.decode().split("\r\n", 1)[0]
    if " 101 " not in status:
        raise ConnectionError("websocket upgrade failed")
    return bytearray(rest)


def _ws_recv(sock, pending):
    def recv_exact(n):
        while len(pending) < n:
            chunk = sock.recv(max(4096, n - len(pending)))
            if not chunk:
                raise ConnectionError("websocket closed")
            pending.extend(chunk)
        out = bytes(pending[:n])
        del pending[:n]
        return out

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
            _ws_send(sock, 0xA, data)
            continue
        if op == 0xA:
            continue
        if op in (0x1, 0x0):
            parts.append(data)
        if fin and parts:
            return b"".join(parts).decode()


def _ws_json(sock, pending):
    raw = _ws_recv(sock, pending)
    if raw is None:
        raise ConnectionError("websocket closed")
    return json.loads(raw)


def ha_commands(sock, host, port, token):
    pending = _ws_handshake(sock, host, port)
    hello = _ws_json(sock, pending)
    if hello.get("type") != "auth_required":
        raise ConnectionError("unexpected websocket hello")
    _ws_send(sock, 0x1, json.dumps({"type": "auth", "access_token": token}).encode())
    auth = _ws_json(sock, pending)
    if auth.get("type") != "auth_ok":
        raise ConnectionError("websocket auth failed")
    jobs = []
    for idx, command in enumerate(HA_WS, start=1):
        jobs.append({"id": idx, "command": command, "items": [], "done": False})
        _ws_send(sock, 0x1, json.dumps({"id": idx, "type": command}).encode())
    by_id = {job["id"]: job for job in jobs}
    next_id = len(jobs) + 1
    while any(not job["done"] for job in jobs):
        msg = _ws_json(sock, pending)
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
        _ws_send(sock, 0x1, json.dumps(follow).encode())
    return jobs[0]["items"], jobs[1]["items"]


def ha_query(url, token):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConnectionError("bad HA_URL")
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    raw = socket.create_connection((host, port), timeout=60)
    raw.settimeout(60)
    sock = raw
    if parsed.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    try:
        return ha_commands(sock, host, port, token)
    finally:
        sock.close()
        if sock is not raw:
            raw.close()


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
        if len(states) != 1:
            log_error(f"{biz_id} {kind} count {len(states)}")
            return
        try:
            number = ha_number(states[0].get("state"))
        except ValueError:
            number = None
        if number is None:
            log_error(f"{biz_id} {kind} not numeric")
            return
        chosen[column] = number
        raw.append(states[0])
    columns = ", ".join(METRIC_COLUMNS)
    placeholders = ", ".join("?" for _ in METRIC_COLUMNS)
    conn = connect()
    try:
        ensure_columns(conn)
        conn.execute(
            f"""
            INSERT INTO readings (
              device_id, source, biz_id, ts, {columns}, raw_json
            ) VALUES (?, ?, ?, ?, {placeholders}, ?)
            """,
            (
                row["id"],
                row["source"],
                biz_id,
                ts,
                *[chosen.get(name) for name in METRIC_COLUMNS],
                json.dumps(raw, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    print(
        f"{ts} {biz_id} temp_c={chosen['temp_c']} humidity={chosen['humidity']}",
        flush=True,
    )


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


def collect():
    if not DB.exists():
        log_error("smartlife.db not found")
        return
    try:
        conn = connect()
    except Exception as exc:
        log_error(f"collect {exc}")
        return
    try:
        ensure_columns(conn)
        rows = conn.execute(
            """
            SELECT id, biz_id, source, address, version, local_key, category
            FROM devices
            """
        ).fetchall()
        if not rows:
            log_error("device not found")
            return
        tuya_rows = []
        ha_rows = []
        for row in rows:
            source = row["source"]
            if source == "ha":
                ha_rows.append(dict(row))
            elif source == "tuya":
                tuya_rows.append(dict(row))
            else:
                log_error(f"unknown source {source}")
    except Exception as exc:
        log_error(f"collect {exc}")
        return
    finally:
        conn.close()
    ha_thread = None
    if ha_rows:
        ha_thread = threading.Thread(target=collect_ha, args=(ha_rows,), name="ha")
        ha_thread.start()
    futures = [POOL.submit(collect_one, row) for row in tuya_rows]
    for future in futures:
        try:
            future.result()
        except Exception as exc:
            log_error(f"collect {exc}")
    if ha_thread is not None:
        ha_thread.join()


def read_status(dev_id, address, version, local_key):
    device = tinytuya.Device(
        dev_id=dev_id,
        address=address,
        local_key=local_key,
    )
    device.set_version(float(version))
    device.set_socketTimeout(5)
    try:
        result = device.status()
    except Exception as exc:
        log_error(f"{dev_id} {exc}")
        return None
    finally:
        device.close()
    if not isinstance(result, dict) or result.get("Error") or result.get("Err"):
        log_error(f"{dev_id} {result}")
        return None
    return result


def field_match(expr, value, lo, hi):
    for part in expr.split(","):
        step = 1
        stepped = False
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            stepped = True
        if step < 1:
            continue
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = int(part)
            end = hi if stepped else start
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def cron_match(expr, dt):
    minute, hour, dom, month, dow = expr.split()
    if not (
        field_match(minute, dt.minute, 0, 59)
        and field_match(hour, dt.hour, 0, 23)
        and field_match(month, dt.month, 1, 12)
    ):
        return False
    dom_ok = field_match(dom, dt.day, 1, 31)
    dow_val = (dt.weekday() + 1) % 7
    dow_ok = field_match(dow, dow_val, 0, 6) or (
        dow_val == 0 and field_match(dow, 7, 0, 7)
    )
    if dom == "*" or dow == "*":
        return dom_ok and dow_ok
    return dom_ok or dow_ok


def next_run(expr, now):
    candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if cron_match(expr, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    raise SystemExit("no matching cron time")


def parse_query(query):
    params = {}
    for part in query.split("&"):
        if not part:
            continue
        key, sep, value = part.partition("=")
        if not sep:
            value = ""
        params.setdefault(unquote(key), []).append(unquote(value))
    return params


def parse_time(value, default):
    if value is None or value == "":
        return default
    if value.isdigit():
        dt = datetime.fromtimestamp(int(value) / 1000, timezone.utc)
    else:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def query_readings(qs):
    now = datetime.now(timezone.utc)
    default_to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    default_from = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        start = parse_time((qs.get("from") or [None])[0], default_from)
        end = parse_time((qs.get("to") or [None])[0], default_to)
    except (ValueError, OSError, OverflowError):
        return 400, {"error": "bad time"}
    name = (qs.get("name") or [None])[0] or None
    source = (qs.get("source") or [None])[0] or None
    if not DB.exists():
        return 503, {"error": "smartlife.db not found"}
    try:
        conn = connect()
    except Exception as exc:
        log_error(f"readings {exc}")
        return 503, {"error": "database"}
    try:
        ensure_columns(conn)
        sql = """
            SELECT d.name, r.source, r.ts, r.co2_ppm, r.co2_state, r.temp_c, r.humidity, r.battery,
                   r.switch, r.pm25, r.filter_pct, r.air_quality, r.fault
            FROM readings r
            JOIN devices d ON d.id = r.device_id
            WHERE r.ts >= ? AND r.ts <= ?
        """
        args = [start, end]
        if name:
            sql += " AND d.name = ?"
            args.append(name)
        if source:
            sql += " AND r.source = ?"
            args.append(source)
        sql += " ORDER BY r.ts ASC"
        rows = conn.execute(sql, args).fetchall()
    except Exception as exc:
        log_error(f"readings {exc}")
        return 503, {"error": "database"}
    finally:
        conn.close()
    return 200, [
        {
            "ts": row["ts"],
            "name": row["name"],
            "source": row["source"],
            "co2_ppm": row["co2_ppm"],
            "co2_state": row["co2_state"],
            "temp_c": row["temp_c"],
            "humidity": row["humidity"],
            "battery": row["battery"],
            "switch": row["switch"],
            "pm25": row["pm25"],
            "filter_pct": row["filter_pct"],
            "air_quality": row["air_quality"],
            "fault": row["fault"],
        }
        for row in rows
    ]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, {"status": "ok"})
            return
        if parsed.path == "/readings":
            code, body = query_readings(parse_query(parsed.query))
            self._send(code, body)
            return
        self._send(404, {"error": "not found"})

    def _send(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


def start_http():
    raw = os.environ.get("HTTP_PORT", "8080").strip()
    if not raw.isdigit():
        raise SystemExit("HTTP_PORT must be an integer")
    port = int(raw)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"listen 0.0.0.0:{port}", flush=True)
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()


def main():
    load_dotenv()
    cron = os.environ.get("COLLECT_CRON", "").strip()
    if len(cron.split()) != 5:
        raise SystemExit("COLLECT_CRON must have 5 fields")
    start_http()
    if os.environ.get("COLLECT_ON_BOOT", "0").strip() == "1":
        collect()
    while True:
        now = datetime.now().astimezone()
        nxt = next_run(cron, now)
        print(f"next {nxt.strftime('%Y-%m-%dT%H:%M:%S%z')}", flush=True)
        while True:
            delay = (nxt - datetime.now().astimezone()).total_seconds()
            if delay <= 0:
                break
            time.sleep(delay)
        woke = datetime.now().astimezone()
        on_slot = woke.replace(second=0, microsecond=0) == nxt.replace(second=0, microsecond=0)
        if on_slot or cron_match(cron, woke):
            collect()
        else:
            log_error(f"missed {nxt.strftime('%Y-%m-%dT%H:%M:%S%z')}")


if __name__ == "__main__":
    main()
