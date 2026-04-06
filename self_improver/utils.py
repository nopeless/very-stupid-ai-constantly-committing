from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class CommandOutcome:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def combined_output(self) -> str:
        data = []
        if self.stdout.strip():
            data.append(self.stdout.strip())
        if self.stderr.strip():
            data.append(self.stderr.strip())
        return "\n".join(data).strip()


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def run_shell_command(
    command: str,
    cwd: Path,
    timeout_seconds: int,
) -> CommandOutcome:
    start = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + f"\nCommand timed out after {timeout_seconds}s"
    duration = time.perf_counter() - start
    return CommandOutcome(
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
    )


def truncate_text(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes]
    return truncated.decode("utf-8", errors="ignore")


def extract_json_object(text: str) -> dict:
    # Try direct parse first.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Then search for the first object block.
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("No JSON object found in model response.")
    return json.loads(match.group(0))


def strip_code_fences(text: str) -> str:
    text = text.strip()
    fence_match = re.match(r"^```[a-zA-Z0-9_-]*\s*([\s\S]*?)\s*```$", text)
    if fence_match:
        return fence_match.group(1).strip()
    return text


def extract_unified_diff(text: str) -> str:
    candidate = strip_code_fences(text)
    lines = candidate.splitlines()

    start_index = None
    for idx, line in enumerate(lines):
        if line.startswith("diff --git ") or line.startswith("--- a/"):
            start_index = idx
            break

    if start_index is None:
        raise ValueError("No unified diff found in model response.")

    diff = "\n".join(lines[start_index:]).rstrip() + "\n"
    if "--- " not in diff or "+++ " not in diff:
        raise ValueError("Diff output is missing unified diff headers.")
    return diff
