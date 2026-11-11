import json
import urllib.error
import urllib.request
from datetime import timedelta
from urllib.parse import quote, urlencode

from smartlife.db import parse_utc, utc_text
from smartlife.log import log_error

LOOKBACK = timedelta(hours=48)
OVERLAP = timedelta(minutes=15)
HORIZON_LAG = timedelta(minutes=2)


def parse_selectors(raw):
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw, list):
        data = raw
    else:
        return None
    if not isinstance(data, list):
        return None
    if any(not isinstance(item, str) or not item for item in data):
        return None
    return data


def history_url(base, entity_id, start, end):
    query = urlencode({"filter_entity_id": entity_id, "end_time": utc_text(end)})
    path = "/api/history/period/" + quote(utc_text(start), safe="")
    return base.rstrip("/") + path + "?" + query + "&skip_initial_state&no_attributes"


def fetch_history(url, token, entity_id, start, end):
    req = urllib.request.Request(
        history_url(url, entity_id, start, end),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise ConnectionError(f"history http {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ConnectionError(f"history {exc.reason}") from exc


def _rows_from_history(payload, device_id, selector):
    if not isinstance(payload, list):
        return None, "history not grouped"
    rows = []
    for group in payload:
        if not isinstance(group, list):
            return None, "history not grouped"
        for item in group:
            if not isinstance(item, dict):
                return None, "history row"
            entity_id = item.get("entity_id")
            state = item.get("state")
            last_changed = item.get("last_changed")
            last_updated = item.get("last_updated")
            if not all(isinstance(value, str) and value for value in (entity_id, last_changed, last_updated)):
                return None, "history row fields"
            if not isinstance(state, str):
                return None, "history row fields"
            try:
                changed = utc_text(last_changed)
                updated = utc_text(last_updated)
            except (ValueError, OSError, OverflowError):
                return None, "history time"
            rows.append((device_id, entity_id, selector, state, changed, updated))
    return rows, None


def _match_entity(entries, biz_id, selector):
    if not isinstance(entries, list):
        return None, "registry missing"
    found = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("device_id") != biz_id:
            continue
        unique_id = entry.get("unique_id")
        if isinstance(unique_id, str) and unique_id.endswith(selector):
            found.append(entry)
    if len(found) != 1:
        return None, f"selector matches {len(found)}"
    entity_id = found[0].get("entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        return None, "selector matches 0"
    return entity_id, None


def sync_one(conn, row, entries, now, fetch):
    biz_id = row.get("biz_id", "")
    raw_selectors = row.get("ha_event_selectors")
    selectors = parse_selectors(raw_selectors)
    if selectors is None:
        if raw_selectors not in (None, "", "[]", []):
            log_error(f"{biz_id} activity selectors invalid")
        return "skip"
    if len(selectors) != 1:
        if selectors:
            log_error(f"{biz_id} activity selectors {len(selectors)}")
        return "skip"
    entity_id, error = _match_entity(entries, biz_id, selectors[0])
    if error:
        if error != "registry missing" or raw_selectors not in (None, "", "[]", []):
            log_error(f"{biz_id} activity {error}")
        return "skip"
    horizon = now - HORIZON_LAG
    checkpoint = row.get("activity_synced_until")
    if checkpoint:
        try:
            synced_at = parse_utc(checkpoint)
        except (ValueError, OSError, OverflowError):
            log_error(f"{biz_id} activity checkpoint invalid")
            return "fail"
        if horizon <= synced_at:
            return "ok"
        floor = horizon - LOOKBACK
        if synced_at < floor:
            log_error(f"{biz_id} activity gap before {utc_text(floor)}")
            start = floor
        else:
            start = synced_at - OVERLAP
    else:
        start = horizon - LOOKBACK
    try:
        payload = fetch(entity_id, start, horizon)
    except Exception as exc:
        log_error(f"{biz_id} activity {exc}")
        return "fail"
    pending, error = _rows_from_history(payload, row["id"], selectors[0])
    if error:
        log_error(f"{biz_id} activity {error}")
        return "fail"
    try:
        conn.executemany(
            """
            INSERT OR IGNORE INTO activity_events (
              device_id, entity_id, selector, state, last_changed, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            pending,
        )
        conn.execute(
            "UPDATE devices SET activity_synced_until = ? WHERE id = ?",
            (utc_text(horizon), row["id"]),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        log_error(f"{biz_id} activity {exc}")
        return "fail"
    return "ok"
