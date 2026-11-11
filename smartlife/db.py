import json
import sqlite3
from datetime import datetime, timezone

from smartlife import paths

READING_COLUMNS = (
    ("switch", "INTEGER"),
    ("pm25", "INTEGER"),
    ("filter_pct", "INTEGER"),
    ("air_quality", "TEXT"),
    ("fault", "INTEGER"),
)
DEVICE_COLUMNS = (
    ("ha_domains", "TEXT"),
    ("ha_event_selectors", "TEXT NOT NULL DEFAULT '[]'"),
    ("activity_synced_until", "TEXT"),
)
ACTIVITY_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS activity_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id INTEGER NOT NULL,
  entity_id TEXT NOT NULL,
  selector TEXT NOT NULL,
  state TEXT NOT NULL,
  last_changed TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  UNIQUE (entity_id, last_updated, state)
)
"""
ACTIVITY_INTERVALS_DDL = """
CREATE TABLE IF NOT EXISTS activity_intervals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  UNIQUE (device_id, kind, start_time)
)
"""
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


def connect():
    conn = sqlite3.connect(paths.DB, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL").fetchone()
    return conn


def ensure_column(conn, table, name, typ):
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def parse_utc(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise ValueError("time")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def utc_text(value):
    dt = parse_utc(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"


def ensure_columns(conn):
    for name, typ in READING_COLUMNS:
        ensure_column(conn, "readings", name, typ)
    for name, typ in DEVICE_COLUMNS:
        ensure_column(conn, "devices", name, typ)
    conn.execute(ACTIVITY_EVENTS_DDL)
    conn.execute(ACTIVITY_INTERVALS_DDL)
    conn.commit()


def insert_reading(row, ts, values, raw):
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
                row["biz_id"],
                ts,
                *[values.get(name) for name in METRIC_COLUMNS],
                json.dumps(raw, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()
