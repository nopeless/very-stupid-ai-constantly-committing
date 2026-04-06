from __future__ import annotations

import json
import sqlite3
import tracemalloc
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
        tracemalloc.start()

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

    def recent_objectives(self, limit: int = 12) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT objective
                FROM iterations
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [str(row[0]) for row in rows if row and row[0]]

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM iterations").fetchone()[0]
            success = conn.execute("SELECT COUNT(*) FROM iterations WHERE success = 1").fetchone()[0]
        current, peak = tracemalloc.get_traced_memory()
        return {
            "total_iterations": int(total),
            "successful_iterations": int(success),
            "current_memory_mb": round(current / 1024 / 1024, 2),
            "peak_memory_mb": round(peak / 1024 / 1024, 2),
        }

    def development_briefing(self, *, window: int = 20) -> str:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT objective, success, error_message
                FROM iterations
                ORDER BY id DESC
                LIMIT ?
                """,
                (window,),
            ).fetchall()

        if not rows:
            return "No prior iterations."

        total = len(rows)
        successes = sum(1 for _, success, _ in rows if success)
        success_rate = (100.0 * successes) / total

        failure_reasons: dict[str, int] = {}
        recent_objectives: list[str] = []
        for objective, success, error_message in rows:
            if objective and len(recent_objectives) < 6:
                recent_objectives.append(str(objective))
            if success:
                continue
            reason = str(error_message or "").strip() or "unknown failure"
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

        top_failures = sorted(failure_reasons.items(), key=lambda item: item[1], reverse=True)[:3]
        failure_text = ", ".join(f"{count}x {reason}" for reason, count in top_failures) if top_failures else "none"
        objectives_text = " | ".join(recent_objectives) if recent_objectives else "none"

        return (
            f"Window={total} iterations; success_rate={success_rate:.1f}%\n"
            f"Recent objectives: {objectives_text}\n"
            f"Top failure reasons: {failure_text}"
        )

    def persist_session_state(self, state_path: Path) -> None:
        """Persist key metrics and recent objectives to a JSON file for session continuity."""
        state = {
            "stats": self.stats(),
            "recent_objectives": self.recent_objectives(limit=12),
            "recent_lessons": self.recent_lessons(limit=8),
            "development_briefing": self.development_briefing(window=20),
        }
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)

    def load_session_state(self, state_path: Path) -> dict:
        """Load session state from a JSON file to restore context across sessions."""
        if not state_path.exists():
            return {}
        with open(state_path, "r") as f:
            return json.load(f)

    def inject_briefing_into_prompt(self, prompt: str, briefing: str) -> str:
        """Inject a concise development briefing into planner prompts."""
        if not briefing:
            return prompt
        return f"{prompt}\n\n--- Development Briefing ---\n{briefing}"

