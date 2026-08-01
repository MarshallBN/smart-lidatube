"""Separately invoked smart poller/worker process (never imported by Gunicorn)."""

import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from smart_lidatube.audit import AuditConfig, AuditWorker
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
    audit_config = AuditConfig(
        enabled=env("SMART_AUDIT_ENABLED", "true").lower() == "true",
        budget_per_hour=int(env("SMART_AUDIT_VERIFY_BUDGET_PER_HOUR", "12")),
        max_token_bank=int(env("SMART_AUDIT_MAX_TOKEN_BANK", "24")),
        fairness_share=float(env("SMART_AUDIT_FAIRNESS_SHARE", "0.20")),
    )
    store.set_setting("audit_enabled", str(audit_config.enabled).lower())
    store.set_setting("audit_budget_per_hour", audit_config.budget_per_hour)
    audit = AuditWorker(store, lidarr, verifier, audit_config)
    return worker, poller, telegram, audit


def run_forever():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    worker, poller, telegram, audit = build_components()
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
            audit.process_once()  # only runs after retry/import work yields idle
            if telegram:
                timezone = ZoneInfo(env("SMART_AUDIT_TIMEZONE", "UTC"))
                now = datetime.now(timezone)
                report_time = env("SMART_AUDIT_REPORT_TIME", "20:00")
                if now.strftime("%H:%M") >= report_time and not worker.store.regular_work_pending():
                    chat = worker.review_chat_id
                    if chat is not None:
                        telegram.send_audit_digest(
                            chat, now.date().isoformat(),
                            env("SMART_AUDIT_REPORT_EMPTY", "false").lower() == "true",
                        )
                telegram.poll_once()
        except Exception:
            LOGGER.exception("smart worker cycle failed")
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()
