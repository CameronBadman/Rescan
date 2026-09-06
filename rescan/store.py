"""SQLite persistence for batches, candidates, analysis runs and the audit trail.

Two things are kept apart on purpose.

A **batch** is a set of resumes: ingested once, extracted, structured and
de-identified once. That work is expensive and has nothing to do with any
particular role.

An **analysis run** is a compiled rule set applied to a batch. It owns its
screening outcomes, its scores and its shortlist, so one batch can be analysed
many times — a second run never overwrites the first, and two runs can be
compared.

The audit table is not incidental. From December 2026 the Privacy Act reforms
require disclosure of automated decisions affecting individuals, so every stage
transition, rule application, rule flag and ensemble tiebreak is written here
with its reason at the time it happens, not reconstructed afterwards. Every row
carries its batch, and run-scoped rows also carry their run, so a run's trail
can be read alone or merged with the ingestion events behind it.

One connection guarded by a lock, with WAL enabled. A hackathon batch is
hundreds of documents, not millions; the bottleneck is inference, not SQLite.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from rescan.config import settings
from rescan.schemas import BatchStatus, CandidateStatus, RunStatus

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id            TEXT PRIMARY KEY,
    name          TEXT,
    status        TEXT NOT NULL,
    source_json   TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id              TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL REFERENCES batches(id),
    filename        TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    status          TEXT NOT NULL,
    candidate_ref   TEXT,
    duplicate_of    TEXT,
    extraction_json TEXT,
    structured_json TEXT,
    anonymized_json TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Dedup is per batch: the same resume in two batches is two candidates, but
-- re-uploading one batch during development does not reprocess it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_batch_hash
    ON candidates(batch_id, content_hash);
CREATE INDEX IF NOT EXISTS idx_candidates_batch_status ON candidates(batch_id, status);

CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    batch_id       TEXT NOT NULL REFERENCES batches(id),
    name           TEXT,
    status         TEXT NOT NULL,
    role_json      TEXT NOT NULL,
    rules_json     TEXT,
    shortlist_json TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_batch ON runs(batch_id, created_at);

-- One row per (run, candidate): what this run concluded about this person.
CREATE TABLE IF NOT EXISTS run_results (
    run_id         TEXT NOT NULL REFERENCES runs(id),
    candidate_id   TEXT NOT NULL REFERENCES candidates(id),
    candidate_ref  TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    screening_json TEXT,
    score_json     TEXT,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (run_id, candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_run_results_run ON run_results(run_id, candidate_ref);

CREATE TABLE IF NOT EXISTS audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     TEXT NOT NULL,
    run_id       TEXT,
    candidate_id TEXT,
    at           TEXT NOT NULL,
    stage        TEXT NOT NULL,
    event        TEXT NOT NULL,
    detail_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit(batch_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit(run_id, id);
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
            self._retire_v1_schema()
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _retire_v1_schema(self) -> None:
        """Move a pre-split database aside rather than reading it wrongly.

        The old schema kept rules, screening and the shortlist on a `jobs` row.
        Its tables are renamed, not dropped, so a stale database is recoverable
        by hand; the service starts on a clean one.
        """
        tables = {
            row["name"]
            for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "jobs" not in tables or "batches" in tables:
            return
        log.warning(
            "%s uses the pre-batch schema; renaming its tables to *_v1 and starting fresh",
            self.db_path,
        )
        for table in ("jobs", "candidates", "audit"):
            if table in tables:
                self._conn.execute(f"DROP TABLE IF EXISTS {table}_v1")
                self._conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------- batches ----------------

    def create_batch(self, batch_id: str, *, name: str | None = None, source: dict[str, Any] | None = None) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO batches (id, name, status, source_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (batch_id, name, BatchStatus.QUEUED.value, _dump(source), now, now),
            )
            self._conn.commit()

    def update_batch(self, batch_id: str, **fields: Any) -> None:
        self._update("batches", batch_id, {"status", "name", "error"}, fields)

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            return None
        batch = dict(row)
        batch["source"] = _load(batch.pop("source_json"))
        batch["counts"] = self.batch_counts(batch_id)
        batch["total"] = sum(batch["counts"].values())
        batch["ready"] = batch["counts"][CandidateStatus.READY.value]
        batch["runs"] = self.list_runs(batch_id=batch_id)
        return batch

    def list_batches(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT b.id, b.name, b.status, b.created_at, b.updated_at,"
                " (SELECT COUNT(*) FROM candidates c WHERE c.batch_id = b.id) AS total,"
                " (SELECT COUNT(*) FROM candidates c WHERE c.batch_id = b.id AND c.status = ?) AS ready,"
                " (SELECT COUNT(*) FROM runs r WHERE r.batch_id = b.id) AS runs"
                " FROM batches b ORDER BY b.created_at DESC LIMIT ?",
                (CandidateStatus.READY.value, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def batch_counts(self, batch_id: str) -> dict[str, int]:
        """Per-stage candidate counts, for the ingestion progress view."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM candidates WHERE batch_id = ? GROUP BY status",
                (batch_id,),
            ).fetchall()
        counts = {status.value: 0 for status in CandidateStatus}
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts

    # ---------------- candidates ----------------

    def add_candidate(
        self, candidate_id: str, batch_id: str, filename: str, content_hash: str
    ) -> str | None:
        """Insert a candidate, or return the existing id when it is a duplicate."""
        now = _now()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM candidates WHERE batch_id = ? AND content_hash = ?",
                (batch_id, content_hash),
            ).fetchone()
            if existing is not None:
                return existing["id"]
            self._conn.execute(
                "INSERT INTO candidates (id, batch_id, filename, content_hash, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (candidate_id, batch_id, filename, content_hash, CandidateStatus.PENDING.value, now, now),
            )
            self._conn.commit()
        return None

    def add_duplicate(
        self, candidate_id: str, batch_id: str, filename: str, content_hash: str, duplicate_of: str
    ) -> None:
        """Record a duplicate submission so the count still reflects the upload."""
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO candidates (id, batch_id, filename, content_hash, status,"
                " duplicate_of, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    batch_id,
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
            "anonymized_json", "error",
        }
        self._update("candidates", candidate_id, allowed, fields)

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return self._hydrate_candidate(row) if row else None

    def list_candidates(self, batch_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM candidates WHERE batch_id = ? ORDER BY created_at, filename",
                (batch_id,),
            ).fetchall()
        return [self._hydrate_candidate(row) for row in rows]

    @staticmethod
    def _hydrate_candidate(row: sqlite3.Row) -> dict[str, Any]:
        candidate = dict(row)
        for key, target in (
            ("extraction_json", "extraction"),
            ("structured_json", "structured"),
            ("anonymized_json", "anonymized"),
        ):
            candidate[target] = _load(candidate.pop(key))
        return candidate

    # ---------------- runs ----------------

    def create_run(
        self, run_id: str, batch_id: str, role: dict[str, Any], *, name: str | None = None
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (id, batch_id, name, status, role_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, batch_id, name, RunStatus.QUEUED.value, _dump(role), now, now),
            )
            self._conn.commit()

    def update_run(self, run_id: str, **fields: Any) -> None:
        self._update("runs", run_id, {"status", "name", "rules_json", "shortlist_json", "error"}, fields)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        run = dict(row)
        run["role"] = _load(run.pop("role_json"))
        run["rules"] = _load(run.pop("rules_json"))
        run["shortlist"] = _load(run.pop("shortlist_json"))
        run["counts"] = self.run_counts(run_id)
        run["screened"] = sum(run["counts"].values())
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM candidates WHERE batch_id = ? AND status = ?",
                (run["batch_id"], CandidateStatus.READY.value),
            ).fetchone()["n"]
        run["total"] = total
        return run

    def list_runs(
        self, batch_id: str | None = None, query: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Run summaries, newest first. `query` matches id, name, role title or batch."""
        sql = (
            "SELECT id, batch_id, name, status, role_json, created_at, updated_at,"
            " shortlist_json IS NOT NULL AS has_shortlist FROM runs"
        )
        params: list[Any] = []
        clauses = []
        if batch_id:
            clauses.append("batch_id = ?")
            params.append(batch_id)
        if query:
            clauses.append("(id LIKE ? OR IFNULL(name, '') LIKE ? OR batch_id LIKE ? OR role_json LIKE ?)")
            params.extend([f"%{query}%"] * 4)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        summaries = []
        for row in rows:
            run = dict(row)
            role = _load(run.pop("role_json")) or {}
            run["role_title"] = role.get("title")
            run["has_shortlist"] = bool(run["has_shortlist"])
            summaries.append(run)
        return summaries

    # ---------------- run results ----------------

    def put_result(
        self,
        run_id: str,
        candidate_id: str,
        candidate_ref: str,
        outcome: str,
        *,
        screening: dict[str, Any] | None = None,
        score: dict[str, Any] | None = None,
    ) -> None:
        """Record (or update) what one run concluded about one candidate."""
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO run_results (run_id, candidate_id, candidate_ref, outcome,"
                " screening_json, score_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id, candidate_id) DO UPDATE SET"
                " candidate_ref = excluded.candidate_ref, outcome = excluded.outcome,"
                " screening_json = COALESCE(excluded.screening_json, run_results.screening_json),"
                " score_json = COALESCE(excluded.score_json, run_results.score_json),"
                " updated_at = excluded.updated_at",
                (run_id, candidate_id, candidate_ref, outcome, _dump(screening), _dump(score), now),
            )
            self._conn.commit()

    def set_result_score(self, run_id: str, candidate_id: str, score: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE run_results SET score_json = ?, updated_at = ? WHERE run_id = ? AND candidate_id = ?",
                (_dump(score), _now(), run_id, candidate_id),
            )
            self._conn.commit()

    def clear_results(self, run_id: str) -> None:
        """Drop a run's outcomes before re-screening it in place."""
        with self._lock:
            self._conn.execute("DELETE FROM run_results WHERE run_id = ?", (run_id,))
            self._conn.commit()

    def list_results(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM run_results WHERE run_id = ? ORDER BY candidate_ref", (run_id,)
            ).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result["screening"] = _load(result.pop("screening_json"))
            result["score"] = _load(result.pop("score_json"))
            results.append(result)
        return results

    def run_counts(self, run_id: str) -> dict[str, int]:
        from rescan.schemas import CandidateOutcome

        with self._lock:
            rows = self._conn.execute(
                "SELECT outcome, COUNT(*) AS n FROM run_results WHERE run_id = ? GROUP BY outcome",
                (run_id,),
            ).fetchall()
        counts = {outcome.value: 0 for outcome in CandidateOutcome}
        for row in rows:
            counts[row["outcome"]] = row["n"]
        return counts

    # ---------------- audit ----------------

    def audit(
        self,
        batch_id: str,
        stage: str,
        event: str,
        *,
        run_id: str | None = None,
        candidate_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit (batch_id, run_id, candidate_id, at, stage, event, detail_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, run_id, candidate_id, _now(), stage, event, _dump(detail or {})),
            )
            self._conn.commit()

    def audit_many(
        self, entries: Iterable[tuple[str, str | None, str | None, str, str, dict[str, Any]]]
    ) -> None:
        """Bulk insert of `(batch_id, run_id, candidate_id, stage, event, detail)`."""
        now = _now()
        rows = [
            (batch_id, run_id, candidate_id, now, stage, event, _dump(detail))
            for batch_id, run_id, candidate_id, stage, event, detail in entries
        ]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO audit (batch_id, run_id, candidate_id, at, stage, event, detail_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def audit_trail(
        self,
        batch_id: str | None = None,
        *,
        run_id: str | None = None,
        include_batch: bool = False,
        candidate_id: str | None = None,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """The decision trail.

        By batch: everything that happened to those documents, every run
        included. By run: that run's own events, plus — with `include_batch` —
        the ingestion events behind them, merged in the order they happened.
        """
        params: list[Any] = []
        if run_id and include_batch:
            where = "(run_id = ? OR (batch_id = (SELECT batch_id FROM runs WHERE id = ?) AND run_id IS NULL))"
            params.extend([run_id, run_id])
        elif run_id:
            where = "run_id = ?"
            params.append(run_id)
        elif batch_id:
            where = "batch_id = ?"
            params.append(batch_id)
        else:
            raise ValueError("audit_trail needs a batch_id or a run_id")
        if candidate_id:
            where += " AND candidate_id = ?"
            params.append(candidate_id)
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM audit WHERE {where} ORDER BY id LIMIT ?", params
            ).fetchall()
        entries = []
        for row in rows:
            entry = dict(row)
            entry["detail"] = _load(entry.pop("detail_json")) or {}
            entries.append(entry)
        return entries

    # ---------------- internals ----------------

    def _update(self, table: str, row_id: str, allowed: set[str], fields: dict[str, Any]) -> None:
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._lock:
            self._conn.execute(
                f"UPDATE {table} SET {assignments}, updated_at = ? WHERE id = ?",
                (*updates.values(), _now(), row_id),
            )
            self._conn.commit()
