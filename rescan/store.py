"""SQLite persistence for jobs, candidates and the audit trail.

The audit table is not incidental. From December 2026 the Privacy Act reforms
require disclosure of automated decisions affecting individuals, so every stage
transition, rule application, rule flag and ensemble tiebreak is written here
with its reason at the time it happens, not reconstructed afterwards.

One connection guarded by a lock, with WAL enabled. A hackathon batch is
hundreds of documents, not millions; the bottleneck is inference, not SQLite.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from rescan.config import settings
from rescan.schemas import CandidateStatus, JobStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    role_json     TEXT NOT NULL,
    rules_json    TEXT,
    shortlist_json TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id              TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL REFERENCES jobs(id),
    filename        TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    status          TEXT NOT NULL,
    candidate_ref   TEXT,
    duplicate_of    TEXT,
    extraction_json TEXT,
    structured_json TEXT,
    anonymized_json TEXT,
    screening_json  TEXT,
    score_json      TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Dedup is per job: the same resume submitted to two roles is two candidates,
-- but re-running one batch during development does not reprocess it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_job_hash
    ON candidates(job_id, content_hash);
CREATE INDEX IF NOT EXISTS idx_candidates_job_status ON candidates(job_id, status);

CREATE TABLE IF NOT EXISTS audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT NOT NULL,
    candidate_id TEXT,
    at           TEXT NOT NULL,
    stage        TEXT NOT NULL,
    event        TEXT NOT NULL,
    detail_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_job ON audit(job_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_candidate ON audit(candidate_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)


def _load(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


class Store:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else settings.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------- jobs ----------------

    def create_job(self, job_id: str, role: dict[str, Any]) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, status, role_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (job_id, JobStatus.QUEUED.value, _dump(role), now, now),
            )
            self._conn.commit()

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"status", "rules_json", "shortlist_json", "error"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {assignments}, updated_at = ? WHERE id = ?",
                (*updates.values(), _now(), job_id),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        job = dict(row)
        job["role"] = _load(job.pop("role_json"))
        job["rules"] = _load(job.pop("rules_json"))
        job["shortlist"] = _load(job.pop("shortlist_json"))
        return job

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, status, created_at, updated_at FROM jobs"
                " ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------------- candidates ----------------

    def add_candidate(
        self, candidate_id: str, job_id: str, filename: str, content_hash: str
    ) -> str | None:
        """Insert a candidate, or return the existing id when it is a duplicate."""
        now = _now()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM candidates WHERE job_id = ? AND content_hash = ?",
                (job_id, content_hash),
            ).fetchone()
            if existing is not None:
                return existing["id"]
            self._conn.execute(
                "INSERT INTO candidates (id, job_id, filename, content_hash, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (candidate_id, job_id, filename, content_hash, CandidateStatus.PENDING.value, now, now),
            )
            self._conn.commit()
        return None

    def add_duplicate(
        self, candidate_id: str, job_id: str, filename: str, content_hash: str, duplicate_of: str
    ) -> None:
        """Record a duplicate submission so the count still reflects the upload."""
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO candidates (id, job_id, filename, content_hash, status,"
                " duplicate_of, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    job_id,
                    filename,
                    f"{content_hash}:dup:{candidate_id}",
                    CandidateStatus.DUPLICATE.value,
                    duplicate_of,
                    now,
                    now,
                ),
            )
            self._conn.commit()

    def update_candidate(self, candidate_id: str, **fields: Any) -> None:
        allowed = {
            "status", "candidate_ref", "extraction_json", "structured_json",
            "anonymized_json", "screening_json", "score_json", "error",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._lock:
            self._conn.execute(
                f"UPDATE candidates SET {assignments}, updated_at = ? WHERE id = ?",
                (*updates.values(), _now(), candidate_id),
            )
            self._conn.commit()

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return self._hydrate_candidate(row) if row else None

    def list_candidates(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM candidates WHERE job_id = ? ORDER BY created_at, filename",
                (job_id,),
            ).fetchall()
        return [self._hydrate_candidate(row) for row in rows]

    @staticmethod
    def _hydrate_candidate(row: sqlite3.Row) -> dict[str, Any]:
        candidate = dict(row)
        for key, target in (
            ("extraction_json", "extraction"),
            ("structured_json", "structured"),
            ("anonymized_json", "anonymized"),
            ("screening_json", "screening"),
            ("score_json", "score"),
        ):
            candidate[target] = _load(candidate.pop(key))
        return candidate

    def status_counts(self, job_id: str) -> dict[str, int]:
        """Per-status counts, for the upload progress view."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM candidates WHERE job_id = ? GROUP BY status",
                (job_id,),
            ).fetchall()
        counts = {status.value: 0 for status in CandidateStatus}
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts

    # ---------------- audit ----------------

    def audit(
        self,
        job_id: str,
        stage: str,
        event: str,
        *,
        candidate_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit (job_id, candidate_id, at, stage, event, detail_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, candidate_id, _now(), stage, event, _dump(detail or {})),
            )
            self._conn.commit()

    def audit_many(self, entries: Iterable[tuple[str, str | None, str, str, dict[str, Any]]]) -> None:
        now = _now()
        rows = [
            (job_id, candidate_id, now, stage, event, _dump(detail))
            for job_id, candidate_id, stage, event, detail in entries
        ]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO audit (job_id, candidate_id, at, stage, event, detail_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def audit_trail(
        self, job_id: str, candidate_id: str | None = None, limit: int = 2000
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM audit WHERE job_id = ?"
        params: list[Any] = [job_id]
        if candidate_id:
            query += " AND candidate_id = ?"
            params.append(candidate_id)
        query += " ORDER BY id LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        entries = []
        for row in rows:
            entry = dict(row)
            entry["detail"] = _load(entry.pop("detail_json")) or {}
            entries.append(entry)
        return entries
