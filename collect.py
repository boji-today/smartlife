import json
import os
import sqlite3
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
        pending = []
        for row in rows:
            source = row["source"]
            if source == "ha":
                # TODO: 用 biz_id 只读拉取 HA 状态并写入 readings。禁止服务调用。
                continue
            if source != "tuya":
                log_error(f"unknown source {source}")
                continue
            pending.append(dict(row))
    except Exception as exc:
        log_error(f"collect {exc}")
        return
    finally:
        conn.close()
    futures = [POOL.submit(collect_one, row) for row in pending]
    for future in futures:
        try:
            future.result()
        except Exception as exc:
            log_error(f"collect {exc}")


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
