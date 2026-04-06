from pathlib import Path

from self_improver.memory import IterationRecord, MemoryStore


def test_memory_store_records_iterations(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    row_id = store.record_iteration(
        IterationRecord(
            started_at="2026-04-06T00:00:00+00:00",
            finished_at="2026-04-06T00:01:00+00:00",
            objective="test objective",
            success=True,
            score_before=100.0,
            score_after=100.0,
            plan_json={"k": "v"},
            validation_json={"passed": True},
            patch_sha256="abc123",
            commit_sha="def456",
            error_message="",
        )
    )
    store.record_lesson(row_id, "small patches pass more reliably")
    summary = store.recent_iteration_summary(limit=1)
    lessons = store.recent_lessons(limit=1)

    assert row_id == 1
    assert "test objective" in summary
    assert lessons == ["small patches pass more reliably"]
