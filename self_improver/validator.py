from __future__ import annotations

import re
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
    # Define dangerous patterns that could cause patch apply failures
    DANGEROUS_PATTERNS = [
        r'\$\{.*\}',  # Variable expansion
        r'\$\(',  # Command substitution
        r'\`.*\`',  # Backtick command substitution
        r'\|',  # Pipe operator
        r'&&',  # AND operator
        r'\|\|',  # OR operator
        r';',  # Semicolon command separator
        r'&',  # Background process
        r'\*',  # Wildcard
        r'\?',  # Wildcard
        r'\[.*\]',  # Character class
        r'\{.*\}',  # Brace expansion
        r'\(',  # Parentheses
        r'\)',  # Parentheses
    ]
    
    # Maximum command length
    MAX_COMMAND_LENGTH = 10000
    
    # Maximum number of commands
    MAX_COMMANDS = 100
    
    def __init__(self, workspace: Path, timeout_seconds: int) -> None:
        self.workspace = workspace
        self.timeout_seconds = timeout_seconds

    def _validate_command(self, command: str) -> None:
        """Validate a single command for safety."""
        # Check for dangerous patterns
        for pattern in self.DANGEROUS_PATTERNS:
            if re.search(pattern, command):
                raise ValueError(f"command contains dangerous pattern: {pattern}")
        
        # Check for file path references that don't exist
        if command.strip():
            # Check for common dangerous file operations
            if re.search(r'(rm\s+-rf\s+|\s+rm\s+-rf\s+)', command):
                raise ValueError("command contains dangerous file deletion pattern")
            if re.search(r'(mv\s+.*\s+.*\s+|\s+mv\s+.*\s+.*\s+)', command):
                raise ValueError("command contains dangerous file move pattern")
            if re.search(r'(cp\s+.*\s+.*\s+|\s+cp\s+.*\s+.*\s+)', command):
                raise ValueError("command contains dangerous file copy pattern")
    
    def run(self, commands: list[str]) -> ValidationReport:
        # Validate input commands to prevent malformed patches
        if not isinstance(commands, list):
            raise ValueError("commands must be a list")
        if len(commands) > self.MAX_COMMANDS:
            raise ValueError(f"too many commands, maximum is {self.MAX_COMMANDS}")
        for command in commands:
            if not isinstance(command, str):
                raise ValueError("each command must be a string")
            if not command.strip():
                raise ValueError("commands cannot be empty")
            # Validate command length to prevent excessively long commands
            if len(command) > self.MAX_COMMAND_LENGTH:
                raise ValueError("command exceeds maximum length of 10000 characters")
            # Validate command does not contain null bytes or invalid characters
            if "\x00" in command:
                raise ValueError("command contains null bytes")
            # Validate command does not contain dangerous patterns
            self._validate_command(command)
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
