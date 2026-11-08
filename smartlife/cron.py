from datetime import timedelta


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
