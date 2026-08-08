"""Separately invoked smart poller/worker process (never imported by Gunicorn)."""

import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from smart_lidatube.audit import AuditConfig, AuditWorker
from smart_lidatube.clients import LidarrClient, NavidromeClient, YouTubeClient
from smart_lidatube.fingerprint import AcoustIDClient, FileVerifier, Fpcalc
from smart_lidatube.guards import HostResourceGuard
from smart_lidatube.retry import PlaylistPoller
from smart_lidatube.remediation import RemediationDispatcher
from smart_lidatube.quality import FFprobe
from smart_lidatube.store import Store
from smart_lidatube.telegram import TelegramBot
from smart_lidatube.worker import JobWorker
from smart_lidatube.slskd import (
    LocalSoularrStateAdapter,
    ProductionConflictStateProvider,
    ReviewGatedDiscoverySources,
    SlskdDiscoveryClient,
    SoularrLidarrConflictGuard,
)


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


def _lidarr_from_config(getenv):
    return LidarrClient(
        getenv("LIDARR_ADDRESS") or "http://lidarr:8686",
        getenv("LIDARR_API_KEY") or "",
        timeout=float(getenv("LIDARR_API_TIMEOUT") or "30"),
    )


def _slskd_from_config(getenv, **kwargs):
    return SlskdDiscoveryClient(
        getenv("SLSKD_URL") or "", getenv("SLSKD_API_KEY") or "",
        timeout=(float(getenv("SLSKD_CONNECT_TIMEOUT") or "2"),
                 float(getenv("SLSKD_READ_TIMEOUT") or "5")),
        result_cap=int(getenv("SLSKD_RESULT_CAP") or "20"),
        poll_attempts=int(getenv("SLSKD_POLL_ATTEMPTS") or "3"),
        opaque_key=getenv("SLSKD_OPAQUE_ID_KEY") or getenv("SLSKD_API_KEY"),
        **kwargs,
    )


class RuntimeSourceStatus:
    def __init__(self, slskd=None, enabled=False):
        self.slskd = slskd
        self.enabled = enabled

    def health(self):
        state = (self.slskd.health() if self.slskd else
                 {"state": "disabled", "error": "not_configured"})
        if self.slskd and not self.enabled:
            state = {"state": "disabled", "error": "coexistence_not_acknowledged"}
        return {"youtube": {"state": "available", "error": None}, "slskd": state}


def build_source_status(getenv=env, *, session=None):
    url, key = getenv("SLSKD_URL") or "", getenv("SLSKD_API_KEY") or ""
    if not url or not key:
        return RuntimeSourceStatus()
    options = {"session": session} if session is not None else {}
    try:
        client = _slskd_from_config(getenv, **options)
    except (ValueError, TypeError):
        return RuntimeSourceStatus()
    acknowledged = getenv("SMART_SOULARR_COEXISTENCE_MODE") == "manual-retry-only"
    return RuntimeSourceStatus(client, acknowledged)


def build_discovery_source(youtube, getenv=env, *, store=None, lidarr=None):
    """Enable slskd only with explicit conservative Soularr acknowledgement."""
    slskd_url = getenv("SLSKD_URL") or ""
    slskd_key = getenv("SLSKD_API_KEY") or ""
    coexistence = getenv("SMART_SOULARR_COEXISTENCE_MODE") or ""
    if not all((slskd_url, slskd_key)) or coexistence != "manual-retry-only":
        return youtube
    slskd = _slskd_from_config(getenv)
    store = store or Store(getenv("SMART_DB_PATH") or "/lidatube/config/smart-lidatube.db")
    conflict = ProductionConflictStateProvider(
        lidarr or _lidarr_from_config(getenv), LocalSoularrStateAdapter(store, coexistence)
    )
    return ReviewGatedDiscoverySources(
        youtube, slskd, SoularrLidarrConflictGuard(conflict)
    )


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
    source = build_discovery_source(
        YouTubeClient(cookies=env("YTDLP_COOKIES", "") or None), store=store, lidarr=lidarr
    )
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
        auto_max_attempts=(int(env("SMART_AUTO_MAX_ATTEMPTS"))
                           if env("SMART_AUTO_MAX_ATTEMPTS") else None),
        import_verify_interval=float(env("SMART_IMPORT_VERIFY_INTERVAL", "10")),
        import_verify_timeout=float(env("SMART_IMPORT_VERIFY_TIMEOUT", "900")),
        candidate_probe=FFprobe(timeout=float(env("SMART_FFPROBE_TIMEOUT", "10"))),
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
    legacy_audit_budget = env("SMART_AUDIT_VERIFY_BUDGET_PER_HOUR", "")
    audit_max = env("SMART_AUDIT_MAX_PER_HOUR", legacy_audit_budget or "300")
    audit_config = AuditConfig(
        enabled=env("SMART_AUDIT_ENABLED", "true").lower() == "true",
        budget_per_hour=int(legacy_audit_budget or "12"),
        max_per_hour=int(audit_max),
        max_token_bank=int(env("SMART_AUDIT_MAX_TOKEN_BANK", "24")),
        fairness_share=float(env("SMART_AUDIT_FAIRNESS_SHARE", "0.20")),
        bootstrap_batch_size=int(env("SMART_AUDIT_BOOTSTRAP_BATCH_SIZE", "100")),
        timezone=env("SMART_AUDIT_TIMEZONE", "UTC"),
    )
    store.set_setting("audit_enabled", str(audit_config.enabled).lower())
    store.set_setting("audit_budget_per_hour", audit_config.max_per_hour)
    persisted_mode = store.get_setting("audit_mode")
    configured_mode = env("SMART_AUDIT_MODE", "observe")
    store.set_setting("audit_mode", persisted_mode if persisted_mode in {"observe", "paused"}
                      else configured_mode if configured_mode in {"observe", "paused"} else "observe")
    store.set_setting("app_version", env("SMART_VERSION", "source"))
    candidate_budget = 0
    store.set_setting("candidate_discovery_budget_per_hour", candidate_budget)
    audit = AuditWorker(
        store,
        lidarr,
        verifier,
        audit_config,
        lidarr_music_root=env("LIDARR_MUSIC_ROOT") or None,
        audit_music_root=env("SMART_AUDIT_MUSIC_ROOT") or None,
        probe=FFprobe(timeout=float(env("SMART_FFPROBE_TIMEOUT", "10"))),
        health_check=lidarr.health_check,
        resource_check=HostResourceGuard(
            max_load_per_cpu=float(env("SMART_AUDIT_MAX_LOAD_PER_CPU", "0.75")),
            max_disk_io_ms=int(env("SMART_AUDIT_MAX_DISK_IO_MS", "250")),
        ),
    )
    worker.audit_worker = audit
    worker.remediation_dispatcher = RemediationDispatcher(
        store,
        budget_per_hour=candidate_budget,
        max_token_bank=int(env("SMART_AUDIT_CANDIDATE_SEARCH_MAX_TOKEN_BANK", "2")),
    )
    return worker, poller, telegram


def run_forever():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    worker, poller, telegram = build_components()
    audit = worker.audit_worker
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
            if not worker.store.audit_work_pending():
                audit.bootstrap_once()
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
