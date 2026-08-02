"""Durable, concurrency-safe SQLite state for smart retries."""
import json
import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class Store:
    def __init__(self, path):
        self.path=str(path); Path(self.path).parent.mkdir(parents=True,exist_ok=True); self._migrate()

    def _connect(self):
        c=sqlite3.connect(self.path,timeout=30); c.row_factory=sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=30000"); return c

    def _migrate(self):
        with self._connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
            CREATE TABLE IF NOT EXISTS retry_jobs(id INTEGER PRIMARY KEY,lidarr_track_id INTEGER NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,mode TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',metadata TEXT NOT NULL DEFAULT '{}',prior_source TEXT NOT NULL DEFAULT 'unknown',created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,last_error TEXT,claimed_at TEXT);
            CREATE TABLE IF NOT EXISTS source_attempts(id INTEGER PRIMARY KEY,job_id INTEGER NOT NULL REFERENCES retry_jobs(id),provider TEXT NOT NULL,source_id TEXT NOT NULL,provenance TEXT NOT NULL DEFAULT '{}',verdict TEXT NOT NULL DEFAULT 'pending',evidence TEXT NOT NULL DEFAULT '{}',staged_path TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS source_rejections(lidarr_track_id INTEGER NOT NULL,provider TEXT NOT NULL,source_id TEXT NOT NULL,attempt_id INTEGER REFERENCES source_attempts(id),created_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(lidarr_track_id,provider,source_id));
            CREATE TABLE IF NOT EXISTS ingestion_occurrences(id INTEGER PRIMARY KEY,occurrence_key TEXT NOT NULL,job_id INTEGER NOT NULL REFERENCES retry_jobs(id),consumed_at TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE UNIQUE INDEX IF NOT EXISTS active_occurrence ON ingestion_occurrences(occurrence_key) WHERE consumed_at IS NULL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS library_audit_tracks(lidarr_track_id INTEGER PRIMARY KEY,status TEXT NOT NULL DEFAULT 'never_checked',priority_score INTEGER NOT NULL DEFAULT 0,last_checked_at TEXT,next_check_at TEXT,check_count INTEGER NOT NULL DEFAULT 0,last_file_marker TEXT,last_verifier_version TEXT,evidence_json TEXT NOT NULL DEFAULT '{}',last_error_code TEXT,last_candidate_search_at TEXT,do_not_audit INTEGER NOT NULL DEFAULT 0,do_not_upgrade INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS library_audit_eligible ON library_audit_tracks(do_not_audit,next_check_at,priority_score,last_checked_at);
            CREATE TABLE IF NOT EXISTS library_audit_events(id INTEGER PRIMARY KEY,lidarr_track_id INTEGER NOT NULL REFERENCES library_audit_tracks(lidarr_track_id),event_type TEXT NOT NULL,result_status TEXT NOT NULL,evidence_json TEXT NOT NULL DEFAULT '{}',audit_local_day TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS library_audit_events_report ON library_audit_events(audit_local_day,result_status,lidarr_track_id);
            CREATE TABLE IF NOT EXISTS remediation_queue(id INTEGER PRIMARY KEY,lidarr_track_id INTEGER NOT NULL UNIQUE,reason TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'eligible',job_id INTEGER REFERENCES retry_jobs(id),created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS remediation_queue_eligible ON remediation_queue(status,id);
            """)
            self._add_columns(c, "retry_jobs", {
                "updated_at": "TEXT", "last_error": "TEXT", "claimed_at": "TEXT",
                "claim_token": "TEXT", "retry_count": "INTEGER NOT NULL DEFAULT 0",
                "next_attempt_at": "TEXT", "prior_track_file_id": "INTEGER",
                "submitted_path": "TEXT", "import_result": "TEXT",
                "import_started_at": "TEXT", "import_phase": "TEXT",
                "import_prepared_at": "TEXT", "import_submitted_at": "TEXT",
                "import_checked_at": "TEXT", "notification_attempt_id": "INTEGER",
                "notification_chat_id": "INTEGER", "notification_text": "TEXT",
                "notification_evidence": "TEXT",
            })
            self._add_columns(c,"source_attempts",{"staged_path":"TEXT","updated_at":"TEXT","artifact_manifest":"TEXT"})
            self._add_columns(c,"remediation_queue",{"job_id":"INTEGER REFERENCES retry_jobs(id)"})
            self._add_columns(c,"library_audit_events",{"audit_local_day":"TEXT"})
            # Rebuild this index after adding the local-day reporting key.
            c.execute("DROP INDEX IF EXISTS library_audit_events_report")
            c.execute("CREATE INDEX library_audit_events_report ON library_audit_events(audit_local_day,result_status,lidarr_track_id)")
            # Repoint rejection FKs to the survivor before deleting legacy duplicates.
            c.execute("""UPDATE source_rejections SET attempt_id=(SELECT MIN(a2.id) FROM source_attempts a1 JOIN source_attempts a2 ON a2.job_id=a1.job_id AND a2.provider=a1.provider AND a2.source_id=a1.source_id WHERE a1.id=source_rejections.attempt_id) WHERE attempt_id IS NOT NULL""")
            c.execute("DELETE FROM source_attempts WHERE id NOT IN (SELECT MIN(id) FROM source_attempts GROUP BY job_id,provider,source_id)")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS attempts_job_source ON source_attempts(job_id,provider,source_id)")

    @staticmethod
    def _add_columns(c,table,columns):
        existing={r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for name,definition in columns.items():
            if name not in existing: c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def enqueue_job(self,track_id,idempotency_key,mode="auto",metadata=None,prior_source="unknown"):
        with self._connect() as c:
            c.execute("INSERT OR IGNORE INTO retry_jobs(lidarr_track_id,idempotency_key,mode,metadata,prior_source) VALUES(?,?,?,?,?)",(track_id,idempotency_key,mode,json.dumps(metadata or {}),prior_source))
            return c.execute("SELECT id FROM retry_jobs WHERE idempotency_key=?",(idempotency_key,)).fetchone()["id"]

    def enqueue_occurrence(self,track_id,key,mode,metadata):
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT job_id FROM ingestion_occurrences WHERE occurrence_key=? AND consumed_at IS NULL",(key,)).fetchone()
            if row: return row["job_id"],False
            unique=f"{key}:generation:{uuid4()}"
            cur=c.execute("INSERT INTO retry_jobs(lidarr_track_id,idempotency_key,mode,metadata,prior_source) VALUES(?,?,?,?,?)",(track_id,unique,mode,json.dumps(metadata),"unknown"))
            job=cur.lastrowid; c.execute("INSERT INTO ingestion_occurrences(occurrence_key,job_id) VALUES(?,?)",(key,job)); return job,True

    def consume_occurrence(self,job_id):
        with self._connect() as c: c.execute("UPDATE ingestion_occurrences SET consumed_at=CURRENT_TIMESTAMP WHERE job_id=? AND consumed_at IS NULL",(job_id,))

    def claim_job(self,lease_seconds=300):
        self.recover_stale(lease_seconds)
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT id FROM retry_jobs WHERE status IN ('ready_import','queued') AND (next_attempt_at IS NULL OR next_attempt_at<=CURRENT_TIMESTAMP) ORDER BY CASE status WHEN 'ready_import' THEN 0 ELSE 1 END,id LIMIT 1").fetchone()
            if not row:return None
            token=str(uuid4())
            changed=c.execute("UPDATE retry_jobs SET status='processing',claim_token=?,claimed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('queued','ready_import')",(token,row["id"])).rowcount
            return self._decode(c.execute("SELECT * FROM retry_jobs WHERE id=?",(row["id"],)).fetchone(),("metadata","import_result")) if changed else None

    def recover_stale(self,seconds=300):
        with self._connect() as c:
            return c.execute("UPDATE retry_jobs SET status=CASE WHEN EXISTS(SELECT 1 FROM source_attempts a WHERE a.job_id=retry_jobs.id AND a.verdict='manual_accepted') THEN 'ready_import' ELSE 'queued' END,claim_token=NULL,claimed_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE status='processing' AND claimed_at < datetime('now',?)",(f"-{int(seconds)} seconds",)).rowcount

    def update_job(self,job_id,status,error=None):
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE retry_jobs SET status=?,last_error=?,claim_token=NULL,claimed_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(status,error,job_id))
            c.execute("UPDATE remediation_queue SET status=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=?",(status,job_id))

    def schedule_retry(self,job_id,error,delay=30,max_attempts=5):
        with self._connect() as c:
            row=c.execute("SELECT retry_count FROM retry_jobs WHERE id=?",(job_id,)).fetchone(); count=row[0]+1
            status="failed" if count>=max_attempts else "queued"
            c.execute("UPDATE retry_jobs SET status=?,retry_count=?,next_attempt_at=datetime('now',?),last_error=?,claim_token=NULL,claimed_at=NULL WHERE id=?",(status,count,f"+{int(delay)} seconds",error,job_id))
            c.execute("UPDATE remediation_queue SET status=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=?",(status,job_id))

    def prepare_import(self, job_id, prior_file_id, path):
        with self._connect() as c:
            c.execute(
                """UPDATE retry_jobs SET status='importing',import_phase='prepared',
                prior_track_file_id=?,submitted_path=?,import_result=NULL,
                import_started_at=CURRENT_TIMESTAMP,import_prepared_at=CURRENT_TIMESTAMP,
                import_submitted_at=NULL,import_checked_at=NULL,claim_token=NULL,
                claimed_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (prior_file_id, str(path), job_id),
            )

    def mark_import_submitted(self, job_id, result):
        with self._connect() as c:
            c.execute(
                """UPDATE retry_jobs SET import_phase='submitted',import_result=?,
                import_submitted_at=CURRENT_TIMESTAMP,import_checked_at=NULL,
                updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='importing'
                AND import_phase='prepared'""",
                (json.dumps(result), job_id),
            )

    def mark_import_checked(self, job_id):
        with self._connect() as c:
            c.execute(
                "UPDATE retry_jobs SET import_checked_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (job_id,),
            )

    def begin_import(self, job_id, prior_file_id, path, result):
        """Backward-compatible helper that records an already submitted import."""
        self.prepare_import(job_id, prior_file_id, path)
        self.mark_import_submitted(job_id, result)

    def retry_prepared_import(self, job_id):
        """Safely requeue only a known-unsubmitted prepared import."""
        with self._connect() as c:
            changed = c.execute(
                """UPDATE retry_jobs SET status='ready_import',last_error=NULL,
                claim_token=NULL,claimed_at=NULL,prior_track_file_id=NULL,
                submitted_path=NULL,import_result=NULL,import_started_at=NULL,
                import_phase=NULL,import_prepared_at=NULL,import_submitted_at=NULL,
                import_checked_at=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='import_attention' AND import_phase='prepared'
                AND import_result IS NULL AND import_submitted_at IS NULL""",
                (job_id,),
            ).rowcount
        return bool(changed)

    def record_import_result(self,job_id,result):
        with self._connect() as c:c.execute("UPDATE retry_jobs SET import_result=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(json.dumps(result),job_id))

    def list_importing(self):
        with self._connect() as c: rows=c.execute("SELECT * FROM retry_jobs WHERE status='importing' ORDER BY id").fetchall()
        return [self._decode(r,("metadata","import_result")) for r in rows]

    def prepare_notification(self, job_id, attempt_id, chat_id, text, evidence):
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "UPDATE source_attempts SET verdict='notification_pending',updated_at=CURRENT_TIMESTAMP WHERE id=? AND job_id=?",
                (attempt_id, job_id),
            )
            c.execute(
                """UPDATE retry_jobs SET status='notification_pending',
                notification_attempt_id=?,notification_chat_id=?,notification_text=?,
                notification_evidence=?,last_error=NULL,claim_token=NULL,claimed_at=NULL,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (attempt_id, chat_id, text, json.dumps(evidence), job_id),
            )
            c.execute("UPDATE remediation_queue SET status='notification_pending',updated_at=CURRENT_TIMESTAMP WHERE job_id=?",(job_id,))

    def list_pending_notifications(self):
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM retry_jobs WHERE status='notification_pending' ORDER BY id"
            ).fetchall()
        return [self._decode(r, ("metadata", "notification_evidence")) for r in rows]

    def notification_delivered(self, job_id, attempt_id):
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            changed = c.execute(
                """UPDATE retry_jobs SET status='awaiting_review',last_error=NULL,
                updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='notification_pending'
                AND notification_attempt_id=?""",
                (job_id, attempt_id),
            ).rowcount
            if changed:
                c.execute(
                    "UPDATE source_attempts SET verdict='awaiting_review',updated_at=CURRENT_TIMESTAMP WHERE id=? AND job_id=? AND verdict='notification_pending'",
                    (attempt_id, job_id),
                )
                c.execute("UPDATE remediation_queue SET status='awaiting_review',updated_at=CURRENT_TIMESTAMP WHERE job_id=?",(job_id,))
            return bool(changed)

    def list_jobs_for_track(self, track_id):
        with self._connect() as c:
            rows = c.execute("SELECT * FROM retry_jobs WHERE lidarr_track_id=? ORDER BY id", (track_id,)).fetchall()
        return [self._decode(row, ("metadata", "import_result")) for row in rows]

    def get_job(self,job_id):
        with self._connect() as c:r=c.execute("SELECT * FROM retry_jobs WHERE id=?",(job_id,)).fetchone()
        return self._decode(r,("metadata","import_result")) if r else None

    def add_attempt(self,job_id,provider,source_id,provenance=None):
        with self._connect() as c:
            c.execute("INSERT OR IGNORE INTO source_attempts(job_id,provider,source_id,provenance) VALUES(?,?,?,?)",(job_id,provider,source_id,json.dumps(provenance or {})))
            return c.execute("SELECT id FROM source_attempts WHERE job_id=? AND provider=? AND source_id=?",(job_id,provider,source_id)).fetchone()["id"]

    def get_attempt(self,attempt_id):
        with self._connect() as c:r=c.execute("SELECT * FROM source_attempts WHERE id=?",(attempt_id,)).fetchone()
        return self._decode(r,("provenance","evidence","artifact_manifest")) if r else None

    def list_attempts(self,job_id):
        with self._connect() as c:rows=c.execute("SELECT * FROM source_attempts WHERE job_id=? ORDER BY id",(job_id,)).fetchall()
        return [self._decode(r,("provenance","evidence","artifact_manifest")) for r in rows]

    def update_attempt(self,attempt_id,verdict=None,evidence=None,staged_path=None):
        fields=["updated_at=CURRENT_TIMESTAMP"]; vals=[]
        for name,value in (("verdict",verdict),("evidence",json.dumps(evidence) if evidence is not None else None),("staged_path",str(staged_path) if staged_path is not None else None)):
            if value is not None:fields.append(f"{name}=?");vals.append(value)
        with self._connect() as c:c.execute(f"UPDATE source_attempts SET {','.join(fields)} WHERE id=?",(*vals,attempt_id))

    @staticmethod
    def _artifact_manifest(path):
        path=Path(path).resolve(strict=True)
        return {"path":str(path),"size":path.stat().st_size,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}

    def capture_artifact_manifest(self, attempt_id, path):
        manifest=self._artifact_manifest(path)
        with self._connect() as c:
            c.execute("UPDATE source_attempts SET artifact_manifest=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(json.dumps(manifest),attempt_id))
        return manifest

    def artifact_manifest_matches(self, attempt):
        manifest=attempt.get("artifact_manifest")
        if not isinstance(manifest,dict) or not attempt.get("staged_path"):
            return False
        try:
            return manifest == self._artifact_manifest(attempt["staged_path"])
        except (FileNotFoundError, ValueError, OSError):
            return False

    def set_attempt_verdict(self,*args,**kwargs):self.update_attempt(args[0],verdict=args[1],evidence=args[2] if len(args)>2 else kwargs.get("evidence",{}))

    def apply_review(self,attempt_id,action,evidence):
        verdicts={"accept":"manual_accepted","reject":"manual_rejected","cancel":"cancelled"}; statuses={"accept":"ready_import","reject":"queued","cancel":"cancelled"}
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT a.job_id,j.lidarr_track_id,a.provider,a.source_id FROM source_attempts a JOIN retry_jobs j ON j.id=a.job_id WHERE a.id=? AND a.verdict='awaiting_review' AND j.status='awaiting_review'",(attempt_id,)).fetchone()
            if not row:return None
            c.execute("UPDATE source_attempts SET verdict=?,evidence=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(verdicts[action],json.dumps(evidence),attempt_id)); c.execute("UPDATE retry_jobs SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(statuses[action],row["job_id"]))
            if action=="reject":c.execute("INSERT OR IGNORE INTO source_rejections VALUES(?,?,?,?,CURRENT_TIMESTAMP)",(row["lidarr_track_id"],row["provider"],row["source_id"],attempt_id))
            return row["job_id"]

    def apply_audit_review(self, attempt_id, action, evidence):
        """Consume an audit replacement review exactly once, with safe outcomes."""
        outcomes = {
            "accept": ("manual_accepted", "ready_import"),
            "reject": ("manual_rejected", "queued"),
            "ignore_track": ("cancelled", "cancelled"),
            "audit_later": ("deferred", "queued"),
        }
        if action not in outcomes:
            return None
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("""SELECT a.job_id,j.lidarr_track_id,a.provider,a.source_id
                FROM source_attempts a JOIN retry_jobs j ON j.id=a.job_id
                WHERE a.id=? AND a.verdict='awaiting_review' AND j.status='awaiting_review'
                AND json_extract(j.metadata, '$.audit_remediation') IS NOT NULL""",(attempt_id,)).fetchone()
            if not row:
                return None
            verdict, status = outcomes[action]
            c.execute("UPDATE source_attempts SET verdict=?,evidence=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(verdict,json.dumps(evidence),attempt_id))
            c.execute("UPDATE retry_jobs SET status=?,claim_token=NULL,claimed_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(status,row["job_id"]))
            c.execute("UPDATE remediation_queue SET status=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=?",(status,row["job_id"]))
            if action == "reject":
                c.execute("INSERT OR IGNORE INTO source_rejections VALUES(?,?,?,?,CURRENT_TIMESTAMP)",(row["lidarr_track_id"],row["provider"],row["source_id"],attempt_id))
            if action == "ignore_track":
                c.execute("INSERT INTO library_audit_tracks(lidarr_track_id) VALUES(?) ON CONFLICT(lidarr_track_id) DO NOTHING",(row["lidarr_track_id"],))
                c.execute("UPDATE library_audit_tracks SET do_not_upgrade=1,updated_at=CURRENT_TIMESTAMP WHERE lidarr_track_id=?",(row["lidarr_track_id"],))
            return row["job_id"]

    def reject(self,track_id,provider,source_id,attempt_id=None):
        with self._connect() as c:c.execute("INSERT OR IGNORE INTO source_rejections VALUES(?,?,?,?,CURRENT_TIMESTAMP)",(track_id,provider,source_id,attempt_id))
    def is_rejected(self,track_id,provider,source_id):
        with self._connect() as c:return c.execute("SELECT 1 FROM source_rejections WHERE lidarr_track_id=? AND provider=? AND source_id=?",(track_id,provider,source_id)).fetchone() is not None
    def set_setting(self,key,value):
        with self._connect() as c:c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value)))
    def get_setting(self,key,default=None):
        with self._connect() as c:r=c.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
        return r[0] if r else default
    # Audit records are isolated from retry jobs; audit writes can never enqueue imports.
    @staticmethod
    def _safe_audit_evidence(evidence):
        allowed={"reason","confidence","duration_delta","codec","bitrate","sample_rate","marker","artist","title","next_check"}; out={}
        for key,value in (evidence or {}).items():
            if key not in allowed or not isinstance(value,(str,int,float,bool)): continue
            text=str(value).lower()
            if any(bad in text for bad in ("http://","https://","bearer ","token","password","api_key","@")): continue
            out[key]=value
        return out
    def upsert_audit_track(self,track_id,priority_score=0,**facts):
        with self._connect() as c:c.execute("INSERT INTO library_audit_tracks(lidarr_track_id,priority_score) VALUES(?,?) ON CONFLICT(lidarr_track_id) DO UPDATE SET priority_score=excluded.priority_score,updated_at=CURRENT_TIMESTAMP",(track_id,int(priority_score)))
    def get_audit_track(self,track_id):
        with self._connect() as c:row=c.execute("SELECT * FROM library_audit_tracks WHERE lidarr_track_id=?",(track_id,)).fetchone()
        return self._decode(row,("evidence_json",)) if row else None
    def set_audit_exemption(self,track_id,do_not_audit=None,do_not_upgrade=None):
        self.upsert_audit_track(track_id); fields=[]; values=[]
        for key,value in (("do_not_audit",do_not_audit),("do_not_upgrade",do_not_upgrade)):
            if value is not None: fields.append(key+"=?"); values.append(int(bool(value)))
        if fields:
            with self._connect() as c:c.execute("UPDATE library_audit_tracks SET "+",".join(fields)+",updated_at=CURRENT_TIMESTAMP WHERE lidarr_track_id=?",(*values,track_id))
    def set_audit_last_checked(self,track_id,value):
        self.upsert_audit_track(track_id)
        with self._connect() as c:c.execute("UPDATE library_audit_tracks SET last_checked_at=? WHERE lidarr_track_id=?",(value,track_id))
    def list_eligible_audits(self):
        with self._connect() as c:rows=c.execute("SELECT * FROM library_audit_tracks WHERE do_not_audit=0 AND (next_check_at IS NULL OR next_check_at<=CURRENT_TIMESTAMP) ORDER BY priority_score DESC,lidarr_track_id").fetchall()
        return [self._decode(row,("evidence_json",)) for row in rows]
    def select_audit_candidate(self,fairness_share=.2):
        rows=self.list_eligible_audits()
        if not rows:return None
        slot=int(self.get_setting("audit_selection_slot","0")); self.set_setting("audit_selection_slot",slot+1)
        if fairness_share and (slot+1)%max(1,round(1/fairness_share))==0:return sorted(rows,key=lambda r:(r["last_checked_at"] is not None,r["last_checked_at"] or "",r["lidarr_track_id"]))[0]
        return rows[0]
    def regular_work_pending(self):
        active=("queued","processing","ready_import","importing","import_attention","notification_pending","awaiting_review")
        with self._connect() as c:return c.execute("SELECT 1 FROM retry_jobs WHERE status IN (%s) LIMIT 1" % ",".join("?"*len(active)),active).fetchone() is not None
    def record_audit_result(self,track_id,status,evidence=None,next_check_at=None,marker=None,error_code=None,audit_local_day=None):
        self.upsert_audit_track(track_id); safe=self._safe_audit_evidence(evidence)
        audit_local_day = audit_local_day or datetime.now(timezone.utc).date().isoformat()
        with self._connect() as c:
            previous=c.execute("SELECT status FROM library_audit_tracks WHERE lidarr_track_id=?",(track_id,)).fetchone()["status"]
            c.execute("UPDATE library_audit_tracks SET status=?,last_checked_at=CURRENT_TIMESTAMP,next_check_at=?,check_count=check_count+1,last_file_marker=COALESCE(?,last_file_marker),evidence_json=?,last_error_code=?,updated_at=CURRENT_TIMESTAMP WHERE lidarr_track_id=?",(status,next_check_at,marker,json.dumps(safe),error_code,track_id))
            if previous != status:
                c.execute("INSERT INTO library_audit_events(lidarr_track_id,event_type,result_status,evidence_json,audit_local_day) VALUES(?,?,?,?,?)",(track_id,"classification_change",status,json.dumps(safe),audit_local_day))
            reason_map = {
                ("suspect", "recording_mismatch"): "recording_mismatch",
                ("unavailable", "target_file_missing"): "missing_or_corrupt",
            }
            reason = reason_map.get((status, safe.get("reason")))
            exempt = c.execute("SELECT do_not_upgrade FROM library_audit_tracks WHERE lidarr_track_id=?", (track_id,)).fetchone()["do_not_upgrade"]
            if reason and not exempt:
                c.execute("INSERT OR IGNORE INTO remediation_queue(lidarr_track_id,reason) VALUES(?,?)", (track_id, reason))
    def enqueue_remediation(self, track_id, reason):
        """Queue only an explicitly requested or high-confidence audit repair."""
        allowed = {"missing_or_corrupt", "recording_mismatch", "explicit_request", "approved_upgrade"}
        if reason not in allowed:
            return None
        with self._connect() as c:
            c.execute("INSERT OR IGNORE INTO remediation_queue(lidarr_track_id,reason) VALUES(?,?)", (track_id, reason))
            row = c.execute("SELECT id FROM remediation_queue WHERE lidarr_track_id=?", (track_id,)).fetchone()
        return row["id"]

    def get_remediation(self, queue_id):
        with self._connect() as c:
            row=c.execute("SELECT * FROM remediation_queue WHERE id=?",(queue_id,)).fetchone()
        return dict(row) if row else None

    def claim_remediation(self):
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM remediation_queue WHERE status='eligible' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            changed = c.execute("UPDATE remediation_queue SET status='searching',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='eligible'", (row["id"],)).rowcount
            return {"id": row["id"], "lidarr_track_id": row["lidarr_track_id"], "reason": row["reason"]} if changed else None

    def release_remediation(self, queue_id):
        with self._connect() as c:
            return bool(c.execute("UPDATE remediation_queue SET status='eligible',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='searching' AND job_id IS NULL",(queue_id,)).rowcount)

    def create_remediation_job(self, item, idempotency_key, metadata):
        """Atomically attach the claimed audit queue item to its retry job."""
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT * FROM remediation_queue WHERE id=? AND status='searching' AND job_id IS NULL",(item["id"],)).fetchone()
            if not row:
                return None
            job=c.execute("INSERT INTO retry_jobs(lidarr_track_id,idempotency_key,mode,metadata,prior_source) VALUES(?,?,?,?,?)",(row["lidarr_track_id"],idempotency_key,"manual",json.dumps(metadata),"unknown")).lastrowid
            c.execute("UPDATE remediation_queue SET job_id=?,status='queued',updated_at=CURRENT_TIMESTAMP WHERE id=?",(job,row["id"]))
            return job

    def audit_status(self):
        with self._connect() as c:
            total=c.execute("SELECT COUNT(*) FROM library_audit_tracks").fetchone()[0]; checked=c.execute("SELECT COUNT(*) FROM library_audit_tracks WHERE check_count>0").fetchone()[0]; eligible=c.execute("SELECT COUNT(*) FROM library_audit_tracks WHERE do_not_audit=0 AND (next_check_at IS NULL OR next_check_at<=CURRENT_TIMESTAMP)").fetchone()[0]; counts=dict(c.execute("SELECT status,COUNT(*) FROM library_audit_tracks GROUP BY status").fetchall())
        return {"checked_total":checked,"eligible_total":eligible,"total":total,"enabled":self.get_setting("audit_enabled","true")=="true","budget_per_hour":int(self.get_setting("audit_budget_per_hour","12")),"tokens_available":int(float((self.get_setting("audit_tokens", "0:0")).split(":")[0])),**counts}
    def audit_digest_events(self,date):
        with self._connect() as c:rows=c.execute("SELECT lidarr_track_id,result_status,evidence_json FROM library_audit_events WHERE audit_local_day=? AND event_type='classification_change' ORDER BY id",(date,)).fetchall()
        return [self._decode(row,("evidence_json",)) for row in rows]

    @staticmethod
    def _decode(row,fields):
        out=dict(row)
        for f in fields:
            if f in out and out[f] is not None:
                try:out[f]=json.loads(out[f])
                except (ValueError,TypeError):pass
        return out
