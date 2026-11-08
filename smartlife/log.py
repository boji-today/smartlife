from datetime import datetime, timezone

from smartlife import paths


def log_error(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with paths.LOG.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {msg}\n")
