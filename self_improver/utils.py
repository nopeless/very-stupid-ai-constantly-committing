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

    allowed_prefixes = (
        "diff --git ",
        "index ",
        "--- ",
        "+++ ",
        "@@ ",
        "@@",
        "new file mode ",
        "deleted file mode ",
        "old mode ",
        "new mode ",
        "rename from ",
        "rename to ",
        "similarity index ",
        "dissimilarity index ",
        "Binary files ",
        "\\ No newline at end of file",
    )
    collected: list[str] = []
    for line in lines[start_index:]:
        if line == "":
            collected.append(line)
            continue
        if line.startswith(("+", "-", " ")):
            collected.append(line)
            continue
        if line.startswith(allowed_prefixes):
            collected.append(line)
            continue
        break

    diff = "\n".join(collected).rstrip() + "\n"
    if "--- " not in diff or "+++ " not in diff:
        raise ValueError("Diff output is missing unified diff headers.")
    return diff


def push_git_after_commit(
    cwd: Path,
    message: str | None = None,
    force: bool = False,
    timeout_seconds: int = 300,
) -> CommandOutcome:
    """
    Execute git push after a commit to ensure changes are pushed to remote.
    
    Args:
        cwd: Working directory for git operations
        message: Optional commit message to use
        force: Whether to force push
        timeout_seconds: Command timeout in seconds
    
    Returns:
        CommandOutcome with the result of the git push operation
    """
    # First check if there are any commits to push
    status_cmd = run_shell_command(
        command="git status --porcelain",
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    
    if status_cmd.exit_code != 0:
        return status_cmd
    
    # Check if there are uncommitted changes or staged files
    if status_cmd.stdout.strip():
        return CommandOutcome(
            command="git push",
            exit_code=1,
            stdout="",
            stderr=f"No push needed - working directory has uncommitted changes:\n{status_cmd.stdout}",
            duration_seconds=status_cmd.duration_seconds,
        )
    
    # Check if there are commits to push
    log_cmd = run_shell_command(
        command="git log -1 --oneline",
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    
    if log_cmd.exit_code != 0:
        return log_cmd
    
    # Check if there are remote branches to push to
    remote_cmd = run_shell_command(
        command="git remote -v",
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    
    if remote_cmd.exit_code != 0:
        return remote_cmd
    
    # Get current branch
    branch_cmd = run_shell_command(
        command="git rev-parse --abbrev-ref HEAD",
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    
    if branch_cmd.exit_code != 0:
        return branch_cmd
    
    branch = branch_cmd.stdout.strip()
    
    # Check if branch is already pushed
    remote_branch_cmd = run_shell_command(
        command=f"git push --dry-run {branch}",
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    
    if remote_branch_cmd.exit_code == 0:
        return CommandOutcome(
            command=f"git push {branch}",
            exit_code=0,
            stdout="Branch is already pushed to remote - no action needed",
            stderr="",
            duration_seconds=remote_branch_cmd.duration_seconds,
        )
    
    # Execute git push
    push_cmd = run_shell_command(
        command=f"git push {branch}",
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    
    return push_cmd


def get_git_commit_hash(cwd: Path, timeout_seconds: int = 300) -> str:
    """Get the latest commit hash from the repository."""
    result = run_shell_command(
        command="git rev-parse HEAD",
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    
    if result.exit_code != 0:
        raise RuntimeError(f"Failed to get commit hash: {result.stderr}")
    
    return result.stdout.strip()


def get_git_branch_name(cwd: Path, timeout_seconds: int = 300) -> str:
    """Get the current branch name."""
    result = run_shell_command(
        command="git rev-parse --abbrev-ref HEAD",
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    
    if result.exit_code != 0:
        raise RuntimeError(f"Failed to get branch name: {result.stderr}")
    
    return result.stdout.strip()


def get_git_remote_url(cwd: Path, timeout_seconds: int = 300) -> str:
    """Get the remote repository URL."""
    result = run_shell_command(
        command="git remote get-url origin",
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    
    if result.exit_code != 0:
        raise RuntimeError(f"Failed to get remote URL: {result.stderr}")
    
    return result.stdout.strip()
