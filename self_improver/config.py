from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_ALLOWED_PATHS = [
    "self_improver",
    "tests",
    "README.md",
    "self_improver/TODO.md",
    "pyproject.toml",
    ".gitignore",
]

DEFAULT_VALIDATE_COMMANDS = [
    "python -m pytest -q",
]


@dataclass
class RuntimeConfig:
    ollama_base_url: str = "http://100.94.152.3:11434"
    model: str = "qwen3.5:9b"
    workspace: Path = field(default_factory=lambda: Path(".").resolve())
    state_dir: Path = field(default_factory=lambda: Path(".self_improver"))
    allowed_paths: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_PATHS))
    validate_commands: list[str] = field(default_factory=lambda: list(DEFAULT_VALIDATE_COMMANDS))
    max_patch_bytes: int = 64_000
    max_patch_paths: int = 8
    max_patch_hunks: int = 32
    command_timeout_seconds: int = 300
    ollama_healthcheck_timeout_seconds: int = 5
    cycle_sleep_seconds: int = 15
    planner_context_files: int = 40
    planner_context_bytes: int = 24_000
    target_file_context_bytes: int = 14_000
    allow_dirty_worktree: bool = False
    auto_commit_dirty_worktree: bool = True
    auto_init_git: bool = True
    max_consecutive_failures_before_cooldown: int = 5
    cooldown_seconds: int = 60
    todo_enabled: bool = True
    todo_file: str = "self_improver/TODO.md"

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).resolve()
        self.state_dir = self._resolve_under_workspace(self.state_dir)
        self.allowed_paths = [self._normalize_path_entry(p) for p in self.allowed_paths]
        self.validate_commands = [c.strip() for c in self.validate_commands if c and c.strip()]
        if not self.validate_commands:
            self.validate_commands = list(DEFAULT_VALIDATE_COMMANDS)
        self._validate_runtime_config()

    def _resolve_under_workspace(self, path_value: str | Path) -> Path:
        path = Path(path_value)
        if path.is_absolute():
            return path
        return (self.workspace / path).resolve()

    @staticmethod
    def _normalize_path_entry(path_value: str) -> str:
        cleaned = path_value.replace("\\", "/").strip().lstrip("./")
        return cleaned.rstrip("/")

    @property
    def memory_db_path(self) -> Path:
        return self.state_dir / "memory.db"

    @property
    def policy_path(self) -> Path:
        return self.state_dir / "policy.json"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def todo_path(self) -> Path:
        return self._resolve_under_workspace(self.todo_file)

    def _validate_runtime_config(self) -> None:
        if self.max_patch_bytes <= 0:
            raise ValueError("max_patch_bytes must be positive")
        if self.max_patch_paths <= 0:
            raise ValueError("max_patch_paths must be positive")
        if self.max_patch_hunks <= 0:
            raise ValueError("max_patch_hunks must be positive")
        if self.command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        if self.ollama_healthcheck_timeout_seconds <= 0:
            raise ValueError("ollama_healthcheck_timeout_seconds must be positive")
        if self.cycle_sleep_seconds <= 0:
            raise ValueError("cycle_sleep_seconds must be positive")
        if self.planner_context_files <= 0:
            raise ValueError("planner_context_files must be positive")
        if self.planner_context_bytes <= 0:
            raise ValueError("planner_context_bytes must be positive")
        if self.target_file_context_bytes <= 0:
            raise ValueError("target_file_context_bytes must be positive")
        if self.max_consecutive_failures_before_cooldown <= 0:
            raise ValueError("max_consecutive_failures_before_cooldown must be positive")
        if self.cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        if not self.model:
            raise ValueError("model cannot be empty")
        if not self.ollama_base_url:
            raise ValueError("ollama_base_url cannot be empty")
        if not self.allowed_paths:
            raise ValueError("allowed_paths cannot be empty")
        if not self.validate_commands:
            raise ValueError("validate_commands cannot be empty")

    @classmethod
    def from_optional_file(cls, config_path: str | Path | None = None) -> "RuntimeConfig":
        path_from_env = os.getenv("SELF_IMPROVER_CONFIG")
        raw_path = config_path or path_from_env
        if not raw_path:
            config = cls()
            config.apply_env_overrides()
            return config

        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        config = cls(**payload)
        config.apply_env_overrides()
        return config

    def apply_env_overrides(self) -> None:
        ollama = os.getenv("OLLAMA_BASE_URL")
        model = os.getenv("OLLAMA_MODEL")
        if ollama:
            self.ollama_base_url = ollama.strip()
        if model:
            self.model = model.strip()
