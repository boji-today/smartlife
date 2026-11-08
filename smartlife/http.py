import json
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from smartlife import paths
from smartlife.db import connect, ensure_columns
from smartlife.log import log_error


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
    if not paths.DB.exists():
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
