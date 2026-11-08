from datetime import datetime, timezone

import tinytuya

from smartlife.db import insert_reading
from smartlife.log import log_error


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
    finally:
        device.close()
    if not isinstance(result, dict) or result.get("Error") or result.get("Err"):
        raise RuntimeError(result)
    return result


def collect_one(row):
    biz_id = row["biz_id"]
    try:
        result = read_status(biz_id, row["address"], row["version"], row["local_key"])
        dps = result.get("dps")
        if not dps:
            raise RuntimeError("no dps")
        values = metrics(row["category"], dps)
        if values is None:
            raise RuntimeError(f"unknown category {row['category']}")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        insert_reading(row, ts, values, result)
        if row["category"] == "kj":
            print(
                f"{ts} {biz_id} pm25={values.get('pm25')} air_quality={values.get('air_quality')}",
                flush=True,
            )
        else:
            print(f"{ts} {biz_id} co2_ppm={values.get('co2_ppm')}", flush=True)
    except Exception as exc:
        log_error(f"{biz_id} {exc}")
