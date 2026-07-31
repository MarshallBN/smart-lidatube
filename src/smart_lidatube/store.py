"""Durable, concurrency-safe SQLite state for smart retries."""

import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _migrate(self):
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS retry_jobs(
                  id INTEGER PRIMARY KEY,
                  lidarr_track_id INTEGER NOT NULL,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  mode TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'queued',
                  metadata TEXT NOT NULL DEFAULT '{}',
                  prior_source TEXT NOT NULL DEFAULT 'unknown',
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  last_error TEXT,
                  claimed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS source_attempts(
                  id INTEGER PRIMARY KEY,
                  job_id INTEGER NOT NULL REFERENCES retry_jobs(id),
                  provider TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  provenance TEXT NOT NULL DEFAULT '{}',
                  verdict TEXT NOT NULL DEFAULT 'pending',
                  evidence TEXT NOT NULL DEFAULT '{}',
                  staged_path TEXT,
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(job_id, provider, source_id)
                );
                CREATE TABLE IF NOT EXISTS source_rejections(
                  lidarr_track_id INTEGER NOT NULL,
                  provider TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  attempt_id INTEGER REFERENCES source_attempts(id),
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY(lidarr_track_id, provider, source_id)
                );
                """
            )
            # Upgrade databases created by early smart-fork revisions.
            self._add_columns(
                connection,
                "retry_jobs",
                {
                    # SQLite cannot add a column with a non-constant default.
                    "updated_at": "TEXT",
                    "last_error": "TEXT",
                    "claimed_at": "TEXT",
                },
            )
            self._add_columns(
                connection,
                "source_attempts",
                {
                    "staged_path": "TEXT",
                    "updated_at": "TEXT",
                },
            )
            # Older revisions allowed duplicate attempts. Keep the earliest row
            # before enforcing lifecycle uniqueness during migration.
            connection.execute(
                "DELETE FROM source_attempts WHERE id NOT IN ("
                "SELECT MIN(id) FROM source_attempts "
                "GROUP BY job_id, provider, source_id)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS attempts_job_source "
                "ON source_attempts(job_id, provider, source_id)"
            )

    @staticmethod
    def _add_columns(connection, table, columns):
        existing = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )

    def enqueue_job(
        self,
        track_id,
        idempotency_key,
        mode="auto",
        metadata=None,
        prior_source="unknown",
    ):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO retry_jobs"
                "(lidarr_track_id,idempotency_key,mode,metadata,prior_source) "
                "VALUES(?,?,?,?,?)",
                (
                    track_id,
                    idempotency_key,
                    mode,
                    json.dumps(metadata or {}),
                    prior_source,
                ),
            )
            row = connection.execute(
                "SELECT id FROM retry_jobs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            return row["id"]

    def claim_job(self):
        """Atomically claim one queued job or accepted review awaiting import."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id FROM retry_jobs WHERE status IN ('ready_import','queued') "
                "ORDER BY CASE status WHEN 'ready_import' THEN 0 ELSE 1 END, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                "UPDATE retry_jobs SET status='processing', "
                "claimed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status IN ('queued','ready_import')",
                (row["id"],),
            ).rowcount
            if changed != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM retry_jobs WHERE id=?", (row["id"],)
            ).fetchone()
            return self._decode(claimed, ("metadata",))

    def update_job(self, job_id, status, error=None):
        with self._connect() as connection:
            connection.execute(
                "UPDATE retry_jobs SET status=?, last_error=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, error, job_id),
            )

    def get_job(self, job_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM retry_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return self._decode(row, ("metadata",)) if row else None

    def add_attempt(self, job_id, provider, source_id, provenance=None):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO source_attempts"
                "(job_id,provider,source_id,provenance) VALUES(?,?,?,?)",
                (job_id, provider, source_id, json.dumps(provenance or {})),
            )
            row = connection.execute(
                "SELECT id FROM source_attempts "
                "WHERE job_id=? AND provider=? AND source_id=?",
                (job_id, provider, source_id),
            ).fetchone()
            return row["id"]

    def get_attempt(self, attempt_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        return self._decode(row, ("provenance", "evidence")) if row else None

    def list_attempts(self, job_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM source_attempts WHERE job_id=? ORDER BY id", (job_id,)
            ).fetchall()
        return [self._decode(row, ("provenance", "evidence")) for row in rows]

    def update_attempt(
        self, attempt_id, verdict=None, evidence=None, staged_path=None
    ):
        fields = ["updated_at=CURRENT_TIMESTAMP"]
        values = []
        if verdict is not None:
            fields.append("verdict=?")
            values.append(verdict)
        if evidence is not None:
            fields.append("evidence=?")
            values.append(json.dumps(evidence))
        if staged_path is not None:
            fields.append("staged_path=?")
            values.append(str(staged_path))
        values.append(attempt_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE source_attempts SET {', '.join(fields)} WHERE id=?", values
            )

    def set_attempt_verdict(self, attempt_id, verdict, evidence=None):
        self.update_attempt(attempt_id, verdict=verdict, evidence=evidence or {})

    def reject(self, track_id, provider, source_id, attempt_id=None):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO source_rejections "
                "VALUES(?,?,?,?,CURRENT_TIMESTAMP)",
                (track_id, provider, source_id, attempt_id),
            )

    def is_rejected(self, track_id, provider, source_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM source_rejections WHERE "
                "lidarr_track_id=? AND provider=? AND source_id=?",
                (track_id, provider, source_id),
            ).fetchone()
        return row is not None

    @staticmethod
    def _decode(row, fields):
        output = dict(row)
        for field in fields:
            output[field] = json.loads(output[field])
        return output
