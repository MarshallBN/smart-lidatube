"""Durable, concurrency-safe SQLite state for smart retries."""
import json
import sqlite3
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
            self._add_columns(c,"source_attempts",{"staged_path":"TEXT","updated_at":"TEXT"})
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
        with self._connect() as c:c.execute("UPDATE retry_jobs SET status=?,last_error=?,claim_token=NULL,claimed_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(status,error,job_id))

    def schedule_retry(self,job_id,error,delay=30,max_attempts=5):
        with self._connect() as c:
            row=c.execute("SELECT retry_count FROM retry_jobs WHERE id=?",(job_id,)).fetchone(); count=row[0]+1
            status="failed" if count>=max_attempts else "queued"
            c.execute("UPDATE retry_jobs SET status=?,retry_count=?,next_attempt_at=datetime('now',?),last_error=?,claim_token=NULL,claimed_at=NULL WHERE id=?",(status,count,f"+{int(delay)} seconds",error,job_id))

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
            return bool(changed)

    def get_job(self,job_id):
        with self._connect() as c:r=c.execute("SELECT * FROM retry_jobs WHERE id=?",(job_id,)).fetchone()
        return self._decode(r,("metadata","import_result")) if r else None

    def add_attempt(self,job_id,provider,source_id,provenance=None):
        with self._connect() as c:
            c.execute("INSERT OR IGNORE INTO source_attempts(job_id,provider,source_id,provenance) VALUES(?,?,?,?)",(job_id,provider,source_id,json.dumps(provenance or {})))
            return c.execute("SELECT id FROM source_attempts WHERE job_id=? AND provider=? AND source_id=?",(job_id,provider,source_id)).fetchone()["id"]

    def get_attempt(self,attempt_id):
        with self._connect() as c:r=c.execute("SELECT * FROM source_attempts WHERE id=?",(attempt_id,)).fetchone()
        return self._decode(r,("provenance","evidence")) if r else None

    def list_attempts(self,job_id):
        with self._connect() as c:rows=c.execute("SELECT * FROM source_attempts WHERE job_id=? ORDER BY id",(job_id,)).fetchall()
        return [self._decode(r,("provenance","evidence")) for r in rows]

    def update_attempt(self,attempt_id,verdict=None,evidence=None,staged_path=None):
        fields=["updated_at=CURRENT_TIMESTAMP"]; vals=[]
        for name,value in (("verdict",verdict),("evidence",json.dumps(evidence) if evidence is not None else None),("staged_path",str(staged_path) if staged_path is not None else None)):
            if value is not None:fields.append(f"{name}=?");vals.append(value)
        with self._connect() as c:c.execute(f"UPDATE source_attempts SET {','.join(fields)} WHERE id=?",(*vals,attempt_id))

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

    def reject(self,track_id,provider,source_id,attempt_id=None):
        with self._connect() as c:c.execute("INSERT OR IGNORE INTO source_rejections VALUES(?,?,?,?,CURRENT_TIMESTAMP)",(track_id,provider,source_id,attempt_id))
    def is_rejected(self,track_id,provider,source_id):
        with self._connect() as c:return c.execute("SELECT 1 FROM source_rejections WHERE lidarr_track_id=? AND provider=? AND source_id=?",(track_id,provider,source_id)).fetchone() is not None
    def set_setting(self,key,value):
        with self._connect() as c:c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value)))
    def get_setting(self,key,default=None):
        with self._connect() as c:r=c.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
        return r[0] if r else default
    @staticmethod
    def _decode(row,fields):
        out=dict(row)
        for f in fields:
            if f in out and out[f] is not None:
                try:out[f]=json.loads(out[f])
                except (ValueError,TypeError):pass
        return out
