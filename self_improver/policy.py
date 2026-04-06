from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class AdaptivePolicy:
    cycles: int = 0
    success_streak: int = 0
    failure_streak: int = 0
    planner_temperature: float = 0.25
    coder_temperature: float = 0.18
    reviewer_temperature: float = 0.10
    num_predict: int = 2800
    max_patch_bytes: int = 64_000
    unique_objectives_completed: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=True, indent=2)

    @classmethod
    def from_path(cls, path: Path) -> "AdaptivePolicy":
        if not path.exists():
            return cls()
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(**payload)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n", encoding="utf-8")

    def update_after_iteration(self, success: bool, score_delta: float) -> None:
        self.cycles += 1
        if success:
            self.success_streak += 1
            self.failure_streak = 0
            if score_delta >= 0:
                self.planner_temperature = min(0.45, self.planner_temperature + 0.01)
                self.coder_temperature = min(0.30, self.coder_temperature + 0.01)
                self.max_patch_bytes = min(96_000, self.max_patch_bytes + 1_500)
        else:
            self.failure_streak += 1
            self.success_streak = 0
            self.planner_temperature = max(0.08, self.planner_temperature - 0.03)
            self.coder_temperature = max(0.05, self.coder_temperature - 0.03)
            self.max_patch_bytes = max(16_000, self.max_patch_bytes - 3_000)

    def _get_completed_objectives(self) -> set[str]:
        """Return set of completed objective strings from history."""
        history_path = Path(__file__).parent / "objective_history.json"
        if not history_path.exists():
            return set()
        try:
            return set(json.loads(history_path.read_text(encoding="utf-8")).get("objectives", []))
        except (json.JSONDecodeError, KeyError):
            return set()

    def _record_objective(self, objective: str) -> None:
        """Record an objective to history file."""
        history_path = Path(__file__).parent / "objective_history.json"
        history = self._get_completed_objectives()
        history.add(objective)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(json.dumps({"objectives": list(history)}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def mark_objective_complete(self, objective: str) -> None:
        """Mark an objective as complete to prevent re-generation."""
        if objective in self.get_objective_history():
            return
        self._record_objective(objective)
        self.unique_objectives_completed += 1
        self.success_streak += 1
        self.failure_streak = 0

    def get_objective_history(self) -> set[str]:
        """Return set of completed objectives."""
        return self._get_completed_objectives()

