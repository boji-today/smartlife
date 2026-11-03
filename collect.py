import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tinytuya

ROOT = Path(__file__).resolve().parent
DB = ROOT / "smartlife.db"
LOG = ROOT / "collect.log"


def load_dotenv(path=ROOT / ".env"):
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


def collect():
    if not DB.exists():
        log_error("smartlife.db not found")
        return
    try:
        conn = sqlite3.connect(DB)
    except Exception as exc:
        log_error(f"collect {exc}")
        return
    try:
        rows = conn.execute(
            "SELECT id, address, version, local_key FROM devices"
        ).fetchall()
        if not rows:
            log_error("device not found")
            return
        for dev_id, address, version, local_key in rows:
            result = read_status(dev_id, address, version, local_key)
            if result is None:
                continue
            dps = result.get("dps")
            if not dps:
                log_error(f"{dev_id} no dps")
                continue
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                """
                INSERT INTO readings (
                  device_id, ts, co2_ppm, co2_state, temp_c, humidity, battery, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dev_id,
                    ts,
                    dps.get("2"),
                    dps.get("1"),
                    dps.get("18"),
                    dps.get("19"),
                    dps.get("15"),
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            conn.commit()
            print(f"{ts} {dev_id} co2_ppm={dps.get('2')}", flush=True)
    except Exception as exc:
        log_error(f"collect {exc}")
    finally:
        conn.close()


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


def main():
    load_dotenv()
    cron = os.environ.get("COLLECT_CRON", "").strip()
    if len(cron.split()) != 5:
        raise SystemExit("COLLECT_CRON must have 5 fields")
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
