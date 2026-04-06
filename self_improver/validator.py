from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .utils import CommandOutcome, run_shell_command


@dataclass
class ValidationReport:
    commands: list[CommandOutcome]

    @property
    def passed(self) -> bool:
        return all(item.exit_code == 0 for item in self.commands)

    @property
    def score(self) -> float:
        if not self.commands:
            return 0.0
        passed_count = sum(1 for item in self.commands if item.exit_code == 0)
        return (100.0 * passed_count) / len(self.commands)

    def to_json(self) -> dict:
        return {
            "passed": self.passed,
            "score": self.score,
            "commands": [asdict(command) for command in self.commands],
        }


class Validator:
    def __init__(self, workspace: Path, timeout_seconds: int) -> None:
        self.workspace = workspace
        self.timeout_seconds = timeout_seconds

    def run(self, commands: list[str]) -> ValidationReport:
        outcomes: list[CommandOutcome] = []
        for command in commands:
            outcomes.append(
                run_shell_command(
                    command=command,
                    cwd=self.workspace,
                    timeout_seconds=self.timeout_seconds,
                )
            )
        return ValidationReport(commands=outcomes)
