import threading
from concurrent.futures import ThreadPoolExecutor

from smartlife import paths
from smartlife.db import connect, ensure_columns
from smartlife.ha import collect_ha
from smartlife.log import log_error
from smartlife.tuya import collect_one

POOL = ThreadPoolExecutor(max_workers=2)


def collect():
    if not paths.DB.exists():
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
