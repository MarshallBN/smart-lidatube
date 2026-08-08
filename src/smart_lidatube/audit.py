"""Read-only library integrity scheduling and verification."""
from dataclasses import dataclass
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from smart_lidatube.path_mapping import map_lidarr_music_path
from smart_lidatube.quality import ProbeError, media_quality

AUDIT_STATUSES = {"never_checked", "verified", "likely_correct", "suspect", "unverifiable", "unavailable", "exempt"}

def audit_rate_tier(backlog):
    if backlog > 10_000: return 300
    if backlog > 5_000: return 200
    if backlog > 1_000: return 120
    if backlog > 100: return 60
    if backlog > 0: return 24
    return 12

@dataclass
class AuditConfig:
    enabled: bool = True
    budget_per_hour: int = 12
    max_token_bank: int = 24
    fairness_share: float = .20
    bootstrap_batch_size: int = 100
    timezone: str = "UTC"

def classify_verification(result):
    if result.verdict == "accepted": return "verified"
    if result.verdict == "rejected" or result.reason == "duration_mismatch": return "suspect"
    if result.reason in {"fingerprint_error", "acoustid_disabled"}: return "unverifiable"
    return "likely_correct"

def recheck_seconds(status, count=0):
    if status == "verified": return 60 * 60 * 24 * 270
    if status == "likely_correct": return 60 * 60 * 24 * 75
    if status == "unverifiable": return 60 * 60 * 24 * min(56, 7 * (2 ** min(count, 3)))
    if status == "suspect": return 60 * 60 * 24 * 30
    if status == "unavailable": return 60 * 60 * 24 * 7
    return None

class AuditWorker:
    """Audits an already-organized Lidarr target, without source or import APIs."""
    def __init__(self, store, lidarr, verifier, config=None, clock=None,
                 lidarr_music_root=None, audit_music_root=None, probe=None,
                 health_check=None, resource_check=None):
        self.store, self.lidarr, self.verifier = store, lidarr, verifier
        self.config, self.clock = config or AuditConfig(), clock or (lambda: datetime.now(timezone.utc))
        self.lidarr_music_root = lidarr_music_root
        self.audit_music_root = audit_music_root
        self.probe = probe
        self.health_check = health_check or (lambda: True)
        self.resource_check = resource_check or (lambda: True)

    def refresh_throughput(self):
        backlog = self.store.audit_backlog()
        tier = audit_rate_tier(backlog)
        effective = min(max(0, self.config.budget_per_hour), tier)
        eta = math.ceil(backlog / effective * 100) / 100 if effective else None
        self.store.set_audit_throughput(backlog, tier, effective, eta)
        return effective

    def _token(self):
        now=self.clock().timestamp(); raw=self.store.get_setting("audit_tokens")
        tokens, updated = (self.config.max_token_bank, now) if not raw else map(float, raw.split(":"))
        rate = self.refresh_throughput()
        tokens=min(self.config.max_token_bank, tokens+(now-updated)*rate/3600)
        if tokens < 1:
            self.store.set_setting("audit_tokens",f"{tokens}:{now}"); return False
        self.store.set_setting("audit_tokens",f"{tokens-1}:{now}"); return True

    def bootstrap_once(self):
        """Read one Lidarr page and add only tracks that already have a file."""
        enumerate_tracks = getattr(self.lidarr, "list_audit_tracks", None)
        if not enumerate_tracks:
            return 0
        raw_cursor = self.store.get_setting("audit_bootstrap_cursor", "albums:0")
        cursor = raw_cursor if raw_cursor.startswith("albums:") else "albums:0"
        try:
            result = enumerate_tracks(cursor, self.config.bootstrap_batch_size)
        except Exception:
            # The album list itself was unavailable.  Keep its cursor for a
            # retry, but leave a bounded, safe diagnostic rather than silently
            # spinning forever before any durable bootstrap state exists.
            self.store.set_audit_bootstrap_state("failed", "album_list_failed", 1, cursor)
            return 0
        tracks, next_cursor = result[:2]
        bootstrap = result[2] if len(result) > 2 else {"status": "ok", "error": None, "count": 0}
        added = 0
        for track in tracks:
            if track.get("id") is not None and track.get("trackFileId"):
                self.store.upsert_audit_track(track["id"])
                added += 1
        next_cursor = next_cursor if next_cursor is not None else "albums:0"
        self.store.set_audit_bootstrap_state(
            bootstrap.get("status"), bootstrap.get("error"), bootstrap.get("count"), next_cursor
        )
        return added

    def process_once(self):
        if (not self.config.enabled or self.store.get_setting("audit_mode", "observe") == "paused"
                or self.store.audit_work_pending()): return None
        try:
            if not self.health_check():
                raise RuntimeError("unhealthy")
        except Exception:
            self.store.set_setting("audit_backoff_reason", "health_unavailable")
            return None
        try:
            if not self.resource_check():
                raise RuntimeError("busy")
        except Exception:
            self.store.set_setting("audit_backoff_reason", "resource_unavailable")
            return None
        row=self.store.select_audit_candidate(self.config.fairness_share)
        if not row or not self._token(): return None
        track_id=row["lidarr_track_id"]
        try:
            track=self.lidarr.get_track(track_id); identity=self.lidarr.track_identity(track)
            file_id=identity.get("track_file_id") or track.get("trackFileId")
            target=self.lidarr.get_track_file(file_id) if file_id else None
            path=map_lidarr_music_path(
                (target or {}).get("path", ""), self.lidarr_music_root, self.audit_music_root
            )
            if path is None or not path.is_file():
                self.store.invalidate_quality(track_id)
                self._save(track_id,"unavailable",{"reason":"target_file_missing","artist":identity.get("artist", ""),"title":identity.get("title", "")},count=row["check_count"]); return track_id
            marker=f"{path.stat().st_size}:{int(path.stat().st_mtime)}"
            result=self.verifier.verify_file(path,identity); status=classify_verification(result)
            media_info = (target or {}).get("mediaInfo") or {}
            complete = ((media_info.get("audioCodec") or media_info.get("audioFormat")) is not None
                        and all(media_info.get(key) is not None for key in
                                ("containerFormat", "audioBitrate", "audioSampleRate",
                                 "audioBits", "audioChannels", "duration")))
            probed = None
            if self.probe and not complete:
                try:
                    probed = self.probe.probe(path)
                except ProbeError:
                    probed = None
            self.store.upsert_quality(track_id, marker, media_quality(media_info, probed, path.stat().st_size))
            self._save(track_id,status,{"reason":result.reason,"artist":identity.get("artist", ""),"title":identity.get("title", "")},marker,row["check_count"])
        except Exception:
            self._save(track_id,"unverifiable",{"reason":"audit_system_error"},count=row["check_count"])
        return track_id

    def _save(self,track_id,status,evidence,marker=None,count=0):
        seconds=recheck_seconds(status,count)
        next_at=(self.clock()+timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S") if seconds else None
        local_day=self.clock().astimezone(ZoneInfo(self.config.timezone)).date().isoformat()
        self.store.record_audit_result(track_id,status,evidence,next_at,marker,error_code=evidence.get("reason"),audit_local_day=local_day)
