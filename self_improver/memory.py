from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .utils import utc_now


@dataclass
class IterationRecord:
    started_at: str
    finished_at: str
    objective: str
    success: bool
    score_before: float
    score_after: float
    plan_json: dict
    validation_json: dict
    patch_sha256: str
    commit_sha: str
    error_message: str


class MemoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS iterations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    score_before REAL NOT NULL,
                    score_after REAL NOT NULL,
                    plan_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL,
                    patch_sha256 TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    error_message TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    iteration_id INTEGER NOT NULL,
                    lesson TEXT NOT NULL,
                    FOREIGN KEY(iteration_id) REFERENCES iterations(id)
                )
                """
            )
            conn.commit()

    def record_iteration(self, record: IterationRecord) -> int:
        payload = asdict(record)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO iterations (
                    started_at, finished_at, objective, success, score_before, score_after,
                    plan_json, validation_json, patch_sha256, commit_sha, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["started_at"],
                    payload["finished_at"],
                    payload["objective"],
                    1 if payload["success"] else 0,
                    payload["score_before"],
                    payload["score_after"],
                    json.dumps(payload["plan_json"], ensure_ascii=True),
                    json.dumps(payload["validation_json"], ensure_ascii=True),
                    payload["patch_sha256"],
                    payload["commit_sha"],
                    payload["error_message"],
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def record_lesson(self, iteration_id: int, lesson: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO lessons (created_at, iteration_id, lesson)
                VALUES (?, ?, ?)
                """,
                (utc_now(), iteration_id, lesson.strip()),
            )
            conn.commit()

    def recent_lessons(self, limit: int = 8) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT lesson
                FROM lessons
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row[0] for row in rows]

    def recent_iteration_summary(self, limit: int = 10) -> str:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, objective, success, score_before, score_after, error_message
                FROM iterations
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        if not rows:
            return "No prior iterations."

        lines: list[str] = []
        for row in rows:
            iter_id, objective, success, score_before, score_after, error_message = row
            status = "success" if success else "failure"
            suffix = f" err={error_message}" if (error_message and not success) else ""
            lines.append(
                f"#{iter_id} [{status}] score {score_before:.1f}->{score_after:.1f} objective={objective}{suffix}"
            )
        return "\n".join(lines)

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM iterations").fetchone()[0]
            success = conn.execute("SELECT COUNT(*) FROM iterations WHERE success = 1").fetchone()[0]
        return {"total_iterations": int(total), "successful_iterations": int(success)}
