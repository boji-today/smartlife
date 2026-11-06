# 长连接探测：只打印推送，限时退出。不写库，不提供 HTTP。
# TODO: 改用 tinytuya.Monitor，心跳 12 秒，auto_reconnect，退避 60 秒，连接超时 3 秒。
# TODO: 连上和重连成功后各 status() 一次，填满内存快照。
# TODO: 部分 DPS 并入快照；co2、状态、温湿度、电量有变化才插入 readings。
# TODO: raw_json 存合并后的 dps，不为 7、13、31、101、102 加列。
# TODO: 与 collect.py 或容器不要同时连接同一台设备。

import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import tinytuya

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "smartlife.db"
HEARTBEAT_S = 12


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_error(result):
    return isinstance(result, dict) and (result.get("Error") or result.get("Err"))


def load_device():
    if not DB.exists():
        raise SystemExit("smartlife.db not found")
    uri = f"file:{DB}?immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT id, address, version, local_key FROM devices"
        ).fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        raise SystemExit(f"expected 1 device, got {len(rows)}")
    return rows[0]


def listen(device, seconds):
    deadline = time.monotonic() + seconds
    last_hb = 0.0
    heartbeats = 0
    pushes = 0
    started = time.monotonic()
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now - last_hb >= HEARTBEAT_S:
            result = device.heartbeat(nowait=True)
            if is_error(result):
                print(f"{utc_now()} heartbeat {result}", flush=True)
                break
            heartbeats += 1
            if heartbeats == 1:
                print(
                    f"{utc_now()} connected {device.id} {device.address} version={device.version}",
                    flush=True,
                )
            last_hb = now
        result = device.receive()
        if result is None:
            continue
        if is_error(result):
            print(f"{utc_now()} receive {result}", flush=True)
            break
        dps = result.get("dps") if isinstance(result, dict) else None
        if not dps:
            continue
        pushes += 1
        print(f"{utc_now()} push dps={dps}", flush=True)
    elapsed = int(time.monotonic() - started)
    print(f"{utc_now()} summary pushes={pushes} heartbeats={heartbeats} elapsed={elapsed}", flush=True)
    return pushes


def main():
    seconds = 600
    if len(sys.argv) > 1:
        seconds = int(sys.argv[1])
    dev_id, address, version, local_key = load_device()
    device = tinytuya.Device(
        dev_id=dev_id,
        address=address,
        local_key=local_key,
        version=float(version),
        persist=True,
    )
    device.set_socketTimeout(1)
    device.set_socketRetryLimit(1)
    device.set_socketRetryDelay(0)
    try:
        listen(device, seconds)
        result = device.status()
        if is_error(result) or not isinstance(result, dict):
            print(f"{utc_now()} query {result}", flush=True)
            return
        print(f"{utc_now()} query dps={result.get('dps')}", flush=True)
    finally:
        device.close()


if __name__ == "__main__":
    main()
