from pathlib import Path

from self_improver.config import RuntimeConfig
from self_improver.supervisor import SelfImprovementSupervisor
from self_improver.todo import TodoEntry
from self_improver.validator import ValidationReport


def _make_supervisor(tmp_path: Path) -> SelfImprovementSupervisor:
    (tmp_path / "self_improver").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "self_improver" / "supervisor.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "self_improver" / "memory.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "self_improver" / "ollama.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "self_improver" / "todo.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_utils.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    (tmp_path / "tests" / "test_todo.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

    config = RuntimeConfig(
        workspace=tmp_path,
        state_dir=tmp_path / ".self_improver",
        allow_dirty_worktree=True,
        auto_init_git=False,
    )
    return SelfImprovementSupervisor(config)


def test_sanitize_target_files_filters_unsafe_entries(tmp_path: Path) -> None:
    supervisor = _make_supervisor(tmp_path)
    targets = [
        "self_improver/ollama.py",
        "./self_improver/ollama.py",
        "../secrets.txt",
        "C:/windows/system32/drivers/etc/hosts",
        "tests/test_utils.py",
        "non_allowed/file.txt",
    ]
    sanitized = supervisor._sanitize_target_files(targets)
    assert sanitized == ["self_improver/ollama.py", "tests/test_utils.py"]


def test_default_target_files_returns_existing_allowed_files(tmp_path: Path) -> None:
    supervisor = _make_supervisor(tmp_path)
    defaults = supervisor._default_target_files()
    assert defaults
    assert all(path.startswith("self_improver/") or path.startswith("tests/") for path in defaults)


def test_plan_next_iteration_fallback_handles_forced_todo(tmp_path: Path) -> None:
    supervisor = _make_supervisor(tmp_path)

    def _raise_generate(*args: object, **kwargs: object) -> str:
        raise RuntimeError("planner unavailable")

    supervisor.llm.generate = _raise_generate  # type: ignore[assignment]

    plan = supervisor._plan_next_iteration(
        ValidationReport(commands=[]),
        forced_todo=TodoEntry(
            line_index=0,
            raw_line="fix parser in self_improver/ollama.py",
            text="fix parser in self_improver/ollama.py",
        ),
    )

    assert plan.objective.startswith("Resolve TODO:")
    assert plan.todo_text == "fix parser in self_improver/ollama.py"
    assert plan.target_files
