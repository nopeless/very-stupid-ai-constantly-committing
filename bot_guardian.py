#!/usr/bin/env python
"""
UNTouchable guardian launcher.

Design intent:
- This file lives at repo root and is intentionally outside the bot's default
  `allowed_paths`, so autonomous patches should not modify it.
- It runs each cycle in a fresh Python subprocess so code changes made by one
  cycle are loaded by the next cycle automatically.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def _safe_positive_int(value: object, default: int) -> int:
    if not isinstance(value, int):
        return default
    return value if value > 0 else default


def _load_loop_settings(config_path: Path | None) -> tuple[int, int, int]:
    # defaults mirror RuntimeConfig defaults
    cycle_sleep_seconds = 15
    max_failures_before_cooldown = 5
    cooldown_seconds = 60

    if config_path is None or not config_path.exists():
        return cycle_sleep_seconds, max_failures_before_cooldown, cooldown_seconds

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return cycle_sleep_seconds, max_failures_before_cooldown, cooldown_seconds

    cycle_sleep_seconds = _safe_positive_int(payload.get("cycle_sleep_seconds"), cycle_sleep_seconds)
    max_failures_before_cooldown = _safe_positive_int(
        payload.get("max_consecutive_failures_before_cooldown"),
        max_failures_before_cooldown,
    )
    cooldown_seconds = _safe_positive_int(payload.get("cooldown_seconds"), cooldown_seconds)
    return cycle_sleep_seconds, max_failures_before_cooldown, cooldown_seconds


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Immutable guardian process for self_improver cycles")
    parser.add_argument("--config", type=str, default=None, help="Optional config JSON path")
    parser.add_argument("--cycles", type=int, default=0, help="0 means run forever")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve() if args.config else None
    cycle_sleep_seconds, max_failures_before_cooldown, cooldown_seconds = _load_loop_settings(config_path)

    max_cycles = args.cycles if args.cycles > 0 else None
    cycle = 0
    consecutive_failures = 0

    try:
        while max_cycles is None or cycle < max_cycles:
            cycle += 1
            cmd = [sys.executable, "-m", "self_improver", "cycle"]
            if config_path is not None:
                cmd.extend(["--config", str(config_path)])

            print(f"[guardian] cycle {cycle} starting", flush=True)
            completed = subprocess.run(cmd, check=False)

            if completed.returncode == 0:
                consecutive_failures = 0
                print(f"[guardian] cycle {cycle} success", flush=True)
            else:
                consecutive_failures += 1
                print(
                    f"[guardian] cycle {cycle} failed (exit={completed.returncode}); "
                    f"consecutive_failures={consecutive_failures}",
                    flush=True,
                )
                if consecutive_failures >= max_failures_before_cooldown:
                    print(f"[guardian] cooldown {cooldown_seconds}s", flush=True)
                    time.sleep(cooldown_seconds)
                    consecutive_failures = 0

            if max_cycles is None or cycle < max_cycles:
                time.sleep(cycle_sleep_seconds)
    except KeyboardInterrupt:
        print("[guardian] interrupted", flush=True)
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
