from pathlib import Path

import pytest

from self_improver.config import RuntimeConfig
from self_improver.supervisor import ImprovementPlan, SelfImprovementSupervisor
from self_improver.todo import TodoEntry
from self_improver.utils import CommandOutcome
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


def test_plan_next_iteration_sanitizes_invalid_planner_targets(tmp_path: Path) -> None:
    supervisor = _make_supervisor(tmp_path)

    supervisor.llm.generate = lambda *args, **kwargs: (
        '{"objective":"o","rationale":"r","target_files":["../bad.py","tests/test_utils.py"],'
        '"validation_commands":["python -m pytest -q"],"success_metric":"m"}'
    )  # type: ignore[assignment]

    plan = supervisor._plan_next_iteration(ValidationReport(commands=[]))
    assert plan.target_files == ["tests/test_utils.py"]


def test_todo_resolved_requires_passing_validation(tmp_path: Path) -> None:
    supervisor = _make_supervisor(tmp_path)
    entry = TodoEntry(0, "task", "task")
    plan = ImprovementPlan(
        objective="Resolve TODO: task",
        rationale="r",
        target_files=["tests/test_utils.py"],
        validation_commands=["python -m pytest -q"],
        success_metric="m",
        todo_text="task",
    )

    failing_report = ValidationReport(
        commands=[
            CommandOutcome(
                command="pytest",
                exit_code=1,
                stdout="",
                stderr="failed",
                duration_seconds=0.1,
            )
        ]
    )
    passing_report = ValidationReport(
        commands=[
            CommandOutcome(
                command="pytest",
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0.1,
            )
        ]
    )

    unresolved = supervisor._todo_resolved(
        entry,
        plan=plan,
        changed_paths=["tests/test_utils.py"],
        validation_report=failing_report,
    )
    resolved = supervisor._todo_resolved(
        entry,
        plan=plan,
        changed_paths=["tests/test_utils.py"],
        validation_report=passing_report,
    )

    assert unresolved is False
    assert resolved is True


def test_checkpoint_dirty_worktree_creates_commit_when_enabled(tmp_path: Path) -> None:
    supervisor = _make_supervisor(tmp_path)
    supervisor.config.allow_dirty_worktree = False
    supervisor.config.auto_commit_dirty_worktree = True

    messages: list[str] = []
    supervisor.repo.worktree_is_clean = lambda: False  # type: ignore[assignment]

    def _commit_all(message: str) -> str:
        messages.append(message)
        return "abc123"

    supervisor.repo.commit_all = _commit_all  # type: ignore[assignment]
    sha = supervisor._checkpoint_dirty_worktree("bootstrap")

    assert sha == "abc123"
    assert messages == ["bot: dirty checkpoint (bootstrap)"]


def test_checkpoint_dirty_worktree_raises_when_disabled_and_not_allowed(tmp_path: Path) -> None:
    supervisor = _make_supervisor(tmp_path)
    supervisor.config.allow_dirty_worktree = False
    supervisor.config.auto_commit_dirty_worktree = False
    supervisor.repo.worktree_is_clean = lambda: False  # type: ignore[assignment]

    with pytest.raises(RuntimeError):
        supervisor._checkpoint_dirty_worktree("cycle-start")
