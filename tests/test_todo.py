from pathlib import Path

from self_improver.todo import TodoEntry, TodoQueue


def test_peek_reads_top_actionable_entry(tmp_path: Path) -> None:
    todo = tmp_path / "TODO.md"
    todo.write_text(
        "# tasks\n\n- [x] done\n- [ ] first\n- second\n",
        encoding="utf-8",
    )
    queue = TodoQueue(todo)
    entry = queue.peek()

    assert entry is not None
    assert entry.text == "first"


def test_remove_entry_deletes_only_selected_line(tmp_path: Path) -> None:
    todo = tmp_path / "TODO.md"
    todo.write_text("first\nsecond\nthird\n", encoding="utf-8")
    queue = TodoQueue(todo)

    first = queue.peek()
    assert first is not None
    removed = queue.remove_entry(first)

    assert removed is True
    assert todo.read_text(encoding="utf-8") == "second\nthird\n"


def test_remove_entry_fails_when_line_changed(tmp_path: Path) -> None:
    todo = tmp_path / "TODO.md"
    todo.write_text("- task one\n", encoding="utf-8")
    queue = TodoQueue(todo)
    entry = queue.peek()
    assert entry is not None

    todo.write_text("- task one changed\n", encoding="utf-8")
    removed = queue.remove_entry(TodoEntry(entry.line_index, entry.raw_line, entry.text))
    assert removed is False
