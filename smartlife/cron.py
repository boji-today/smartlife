from datetime import datetime

from croniter import croniter


def cron_match(expr, dt):
    return croniter.match(expr, dt)


def next_run(expr, now):
    nxt = croniter(expr, now).get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=now.tzinfo)
    return nxt
