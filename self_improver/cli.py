from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .config import RuntimeConfig
from .supervisor import SelfImprovementSupervisor


def configure_logging(verbose: bool, log_file: Path | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Autonomous self-improving bot supervisor")
    parser.add_argument(
        "command",
        choices=["run", "cycle", "status"],
        help="run forever, run one cycle, or print bot status",
    )
    parser.add_argument("--config", type=str, default=None, help="Path to config JSON file")
    parser.add_argument("--cycles", type=int, default=0, help="For run mode: 0 means forever")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logs")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = RuntimeConfig.from_optional_file(args.config)
    configure_logging(args.verbose, log_file=config.logs_dir / "bot.log")

    supervisor = SelfImprovementSupervisor(config)
    supervisor.bootstrap()

    if args.command == "status":
        payload = supervisor.status()
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    if args.command == "cycle":
        result = supervisor.run_cycle()
        print(
            json.dumps(
                {
                    "success": result.success,
                    "message": result.message,
                    "objective": result.objective,
                    "score_before": result.score_before,
                    "score_after": result.score_after,
                    "commit_sha": result.commit_sha,
                    "patch_sha256": result.patch_sha256,
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 0 if result.success else 1

    max_cycles = None if args.cycles == 0 else args.cycles
    supervisor.run_forever(max_cycles=max_cycles)
    return 0
