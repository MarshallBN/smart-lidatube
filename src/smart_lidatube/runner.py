"""Separately invoked smart poller/worker process (never imported by Gunicorn)."""

import logging
import os
import time

from smart_lidatube.clients import LidarrClient, NavidromeClient, YouTubeClient
from smart_lidatube.fingerprint import AcoustIDClient, FileVerifier, Fpcalc
from smart_lidatube.retry import PlaylistPoller
from smart_lidatube.store import Store
from smart_lidatube.telegram import TelegramBot
from smart_lidatube.worker import JobWorker


LOGGER = logging.getLogger("smart-lidatube-worker")


def env(name, default="", legacy=None):
    """Read uppercase smart names and lowercase legacy LidaTube names."""
    names = [name, name.lower()]
    if legacy:
        names.extend([legacy, legacy.upper()])
    for candidate in names:
        value = os.environ.get(candidate)
        if value not in (None, ""):
            return value
    return default


def csv_ints(value):
    return {int(item.strip()) for item in value.split(",") if item.strip()}


def build_components():
    store = Store(env("SMART_DB_PATH", "/lidatube/config/smart-lidatube.db"))
    lidarr = LidarrClient(
        env("LIDARR_ADDRESS", "http://lidarr:8686", "lidarr_address"),
        env("LIDARR_API_KEY", legacy="lidarr_api_key"),
        timeout=float(env("LIDARR_API_TIMEOUT", "30", "lidarr_api_timeout")),
        navidrome_music_root=env("NAVIDROME_MUSIC_ROOT") or None,
        lidarr_music_root=env("LIDARR_MUSIC_ROOT") or None,
    )
    telegram = None
    token = env("TELEGRAM_BOT_TOKEN")
    chats = csv_ints(env("TELEGRAM_ALLOWED_CHAT_IDS"))
    if token:
        telegram = TelegramBot(
            token,
            store,
            csv_ints(env("TELEGRAM_ALLOWED_USER_IDS")),
            chats,
        )
    acoustid_key = env("ACOUSTID_API_KEY")
    verifier = FileVerifier(Fpcalc(), AcoustIDClient(acoustid_key))
    source = YouTubeClient(cookies=env("YTDLP_COOKIES", "") or None)
    worker = JobWorker(
        store,
        lidarr,
        source,
        verifier,
        env("DOWNLOADS_ROOT", "/lidatube/downloads"),
        lidarr_downloads_root=env("LIDARR_DOWNLOADS_ROOT", "/lidatube/downloads"),
        telegram=telegram,
        review_chat_id=int(env("TELEGRAM_REVIEW_CHAT_ID", "0")) or (
            min(chats) if chats else None
        ),
        lease_seconds=int(env("SMART_CLAIM_TIMEOUT", "300")),
        retry_delay=int(env("SMART_RETRY_DELAY", "30")),
        max_attempts=int(env("SMART_MAX_ATTEMPTS", "5")),
        import_verify_interval=float(env("SMART_IMPORT_VERIFY_INTERVAL", "10")),
        import_verify_timeout=float(env("SMART_IMPORT_VERIFY_TIMEOUT", "900")),
    )
    poller = None
    nav_url = env("NAVIDROME_URL")
    if nav_url:
        navidrome = NavidromeClient(
            nav_url,
            env("NAVIDROME_USER"),
            env("NAVIDROME_PASSWORD"),
        )
        poller = PlaylistPoller(
            navidrome,
            store,
            lidarr.resolve_track_from_navidrome_entry,
        )
    return worker, poller, telegram


def run_forever():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    worker, poller, telegram = build_components()
    interval = float(env("SMART_POLL_INTERVAL", "10"))
    while True:
        try:
            worker.store.set_setting("worker_heartbeat", str(time.time()))
            worker.store.set_setting("worker_status", "running")
            worker.store.recover_stale(worker.lease_seconds)
            worker.reconcile_imports()
            worker.retry_notifications()
            if poller:
                poller.poll_once()
            while worker.process_once() is not None:
                pass
            if telegram:
                telegram.poll_once()
        except Exception:
            LOGGER.exception("smart worker cycle failed")
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()
