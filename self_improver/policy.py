from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
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

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=True, indent=2)

    @classmethod
    def from_path(cls, path: Path) -> "AdaptivePolicy":
        if not path.exists():
            return cls()
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return cls()
        allowed_keys = {item.name for item in fields(cls)}
        filtered_payload = {key: value for key, value in payload.items() if key in allowed_keys}
        return cls(**filtered_payload)

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
