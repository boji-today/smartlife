import os
import time
from datetime import datetime
from pathlib import Path

from smartlife.paths import configure

configure(Path(__file__).resolve().parent)

from smartlife.cron import cron_match, next_run
from smartlife.http import start_http
from smartlife.jobs import collect
from smartlife.log import log_error
from smartlife.paths import load_dotenv


def main():
    load_dotenv()
    cron = os.environ.get("COLLECT_CRON", "").strip()
    if len(cron.split()) != 5:
        raise SystemExit("COLLECT_CRON must have 5 fields")
    start_http()
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
