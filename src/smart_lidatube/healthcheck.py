"""Container health probe for the standalone smart worker."""

import os
import sys
import time

from smart_lidatube.store import Store


def worker_is_healthy(db_path, max_age=60.0):
    try:
        store = Store(db_path)
        heartbeat = float(store.get_setting("worker_heartbeat", "0") or "0")
        status = store.get_setting("worker_status")
        return status == "running" and time.time() - heartbeat <= float(max_age)
    except Exception:
        return False


def main():
    db_path = os.environ.get("SMART_DB_PATH", "/lidatube/config/smart-lidatube.db")
    try:
        max_age = float(os.environ.get("SMART_WORKER_HEALTH_MAX_AGE", "60"))
    except ValueError:
        return 1
    return 0 if worker_is_healthy(db_path, max_age) else 1


if __name__ == "__main__":
    sys.exit(main())
