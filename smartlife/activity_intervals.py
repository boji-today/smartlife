from datetime import timedelta

from smartlife.db import parse_utc, utc_text

OCCUPIED_SELECTOR = "motion_detected_e_2_1008"
OCCUPIED_KIND = "occupied"
OCCUPIED_GAP = timedelta(seconds=600)


def motion_time(state):
    if not isinstance(state, str):
        return None
    text = state.strip()
    if not text or text in {"unknown", "unavailable"}:
        return None
    try:
        return parse_utc(text)
    except (ValueError, OSError, OverflowError):
        return None


def occupied_intervals(states):
    points = []
    seen = set()
    for state in states:
        moment = motion_time(state)
        if moment is None or moment in seen:
            continue
        seen.add(moment)
        points.append(moment)
    points.sort()
    if not points:
        return []
    segments = []
    start = previous = points[0]
    for moment in points[1:]:
        if moment - previous < OCCUPIED_GAP:
            previous = moment
        else:
            segments.append((start, previous + OCCUPIED_GAP))
            start = previous = moment
    segments.append((start, previous + OCCUPIED_GAP))
    return [(utc_text(start), utc_text(end)) for start, end in segments]


def rebuild_occupied(conn, device_id):
    rows = conn.execute(
        """
        SELECT state FROM activity_events
        WHERE device_id = ? AND selector = ?
        """,
        (device_id, OCCUPIED_SELECTOR),
    ).fetchall()
    if not rows:
        return
    intervals = occupied_intervals(row["state"] for row in rows)
    try:
        conn.execute(
            "DELETE FROM activity_intervals WHERE device_id = ? AND kind = ?",
            (device_id, OCCUPIED_KIND),
        )
        conn.executemany(
            """
            INSERT INTO activity_intervals (device_id, kind, start_time, end_time)
            VALUES (?, ?, ?, ?)
            """,
            [(device_id, OCCUPIED_KIND, start, end) for start, end in intervals],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
