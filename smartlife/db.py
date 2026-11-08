import json
import sqlite3

from smartlife import paths

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


def connect():
    conn = sqlite3.connect(paths.DB, timeout=5)
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
