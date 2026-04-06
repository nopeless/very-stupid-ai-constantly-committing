import json
from pathlib import Path

from self_improver.policy import AdaptivePolicy


def test_policy_to_json_is_serializable() -> None:
    policy = AdaptivePolicy()
    payload = json.loads(policy.to_json())

    assert payload["cycles"] == 0
    assert payload["max_patch_bytes"] == 64_000


def test_policy_from_path_ignores_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "cycles": 5,
                "planner_temperature": 0.3,
                "unknown_setting": "ignore-me",
                "completed_objective_hashes": ["legacy"],
            },
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )

    policy = AdaptivePolicy.from_path(path)

    assert policy.cycles == 5
    assert policy.planner_temperature == 0.3
