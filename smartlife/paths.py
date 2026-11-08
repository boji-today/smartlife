import os

ROOT = None
DB = None
LOG = None


def configure(root):
    global ROOT, DB, LOG
    ROOT = root
    DB = root / "data" / "smartlife.db"
    LOG = root / "collect.log"


def load_dotenv(path=None):
    if path is None:
        path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)
