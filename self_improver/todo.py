from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TodoEntry:
    line_index: int
    raw_line: str
    text: str


class TodoQueue:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._counter_path = path.with_name(f"{path.stem}_counter.txt")
        self._counter = self._load_counter()

    def _load_counter(self) -> int:
        try:
            if self._counter_path.exists():
                return int(self._counter_path.read_text(encoding="utf-8").strip())
        except (ValueError, FileNotFoundError):
            pass
        return 0

    def _save_counter(self) -> None:
        self._counter_path.write_text(str(self._counter), encoding="utf-8")

    def peek(self) -> TodoEntry | None:
        for index, line in enumerate(self._read_lines()):
            text = self._parse_task_line(line)
            if text:
                return TodoEntry(line_index=index, raw_line=line, text=text)
        return None

    def _get_completed_count(self) -> int:
        return self._counter

    def _get_total_count(self) -> int:
        return len(self._read_lines())

    def get_completion_rate(self) -> float:
        total = self._get_total_count()
        completed = self._get_completed_count()
        if total == 0:
            return 0.0
        return completed / total

    def remove_entry(self, entry: TodoEntry) -> bool:
        lines = self._read_lines()
        if entry.line_index < 0 or entry.line_index >= len(lines):
            return False
        if lines[entry.line_index] != entry.raw_line:
            return False
        del lines[entry.line_index]
        self._write_lines(lines)
        self._counter += 1
        self._save_counter()
        return True

    def mark_completed(self, entry: TodoEntry) -> bool:
        lines = self._read_lines()
        if entry.line_index < 0 or entry.line_index >= len(lines):
            return False
        if lines[entry.line_index] != entry.raw_line:
            return False
        
        # Mark the task as completed by changing state from " " to "x"
        completed_line = re.sub(
            r"^[-*]\s+\[(?P<state>[ xX])\]\s*(?P<task>.+)$",
            lambda m: f"[-*] [{m.group('state') if m.group('state') == 'x' else 'x'}] {m.group('task')}",
            lines[entry.line_index]
        )
        lines[entry.line_index] = completed_line
        self._write_lines(lines)
        self._counter += 1
        self._save_counter()
        return True

    @staticmethod
    def _parse_task_line(line: str) -> str | None:
        text = line.strip()
        if not text or text.startswith("#"):
            return None

        match = re.match(r"^[-*]\s+\[(?P<state>[ xX])\]\s*(?P<task>.+)$", text)
        if match:
            if match.group("state").lower() == "x":
                return None
            task = match.group("task").strip()
            return task or None

        match = re.match(r"^[-*]\s+(?P<task>.+)$", text)
        if match:
            task = match.group("task").strip()
            return task or None

        match = re.match(r"^\d+\.\s+(?P<task>.+)$", text)
        if match:
            task = match.group("task").strip()
            return task or None

        return text

    def _read_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return self.path.read_text(encoding="utf-8", errors="replace").splitlines()

    def _write_lines(self, lines: list[str]) -> None:
        if not lines:
            self.path.write_text("", encoding="utf-8", newline="\n")
            return
        payload = "\n".join(lines).rstrip() + "\n"
        self.path.write_text(payload, encoding="utf-8", newline="\n")
