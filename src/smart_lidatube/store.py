"""Durable SQLite state for retries and source provenance."""
import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _migrate(self):
        with self._connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS retry_jobs(
              id INTEGER PRIMARY KEY, lidarr_track_id INTEGER NOT NULL,
              idempotency_key TEXT NOT NULL UNIQUE, mode TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued', metadata TEXT NOT NULL DEFAULT '{}',
              prior_source TEXT NOT NULL DEFAULT 'unknown', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS source_attempts(
              id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES retry_jobs(id),
              provider TEXT NOT NULL, source_id TEXT NOT NULL, provenance TEXT NOT NULL DEFAULT '{}',
              verdict TEXT NOT NULL DEFAULT 'pending', evidence TEXT NOT NULL DEFAULT '{}',
              created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS source_rejections(
              lidarr_track_id INTEGER NOT NULL, provider TEXT NOT NULL, source_id TEXT NOT NULL,
              attempt_id INTEGER REFERENCES source_attempts(id), created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(lidarr_track_id, provider, source_id));
            """)

    def enqueue_job(self, track_id, idempotency_key, mode="auto", metadata=None, prior_source="unknown"):
        with self._connect() as c:
            c.execute("INSERT OR IGNORE INTO retry_jobs(lidarr_track_id,idempotency_key,mode,metadata,prior_source) VALUES(?,?,?,?,?)",
                      (track_id,idempotency_key,mode,json.dumps(metadata or {}),prior_source))
            return c.execute("SELECT id FROM retry_jobs WHERE idempotency_key=?",(idempotency_key,)).fetchone()[0]

    def get_job(self, job_id):
        with self._connect() as c: row=c.execute("SELECT * FROM retry_jobs WHERE id=?",(job_id,)).fetchone()
        return self._decode(row, ("metadata",)) if row else None

    def add_attempt(self, job_id, provider, source_id, provenance=None):
        with self._connect() as c:
            cur=c.execute("INSERT INTO source_attempts(job_id,provider,source_id,provenance) VALUES(?,?,?,?)",(job_id,provider,source_id,json.dumps(provenance or {})))
            return cur.lastrowid

    def get_attempt(self, attempt_id):
        with self._connect() as c: row=c.execute("SELECT * FROM source_attempts WHERE id=?",(attempt_id,)).fetchone()
        return self._decode(row,("provenance","evidence")) if row else None

    def set_attempt_verdict(self, attempt_id, verdict, evidence=None):
        with self._connect() as c: c.execute("UPDATE source_attempts SET verdict=?, evidence=? WHERE id=?",(verdict,json.dumps(evidence or {}),attempt_id))

    def reject(self, track_id, provider, source_id, attempt_id=None):
        with self._connect() as c: c.execute("INSERT OR IGNORE INTO source_rejections VALUES(?,?,?,?,CURRENT_TIMESTAMP)",(track_id,provider,source_id,attempt_id))

    def is_rejected(self, track_id, provider, source_id):
        with self._connect() as c: return c.execute("SELECT 1 FROM source_rejections WHERE lidarr_track_id=? AND provider=? AND source_id=?",(track_id,provider,source_id)).fetchone() is not None

    @staticmethod
    def _decode(row, fields):
        out=dict(row)
        for field in fields: out[field]=json.loads(out[field])
        return out
