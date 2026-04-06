from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .utils import truncate_text


@dataclass
class GitCommandResult:
    ok: bool
    stdout: str
    stderr: str


class RepoManager:
    def __init__(self, workspace: Path, state_dir_name: str = ".self_improver") -> None:
        self.workspace = workspace
        self.state_dir_name = state_dir_name

    def _run_git(self, args: list[str]) -> GitCommandResult:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(self.workspace),
            check=False,
            capture_output=True,
            text=True,
        )
        return GitCommandResult(
            ok=completed.returncode == 0,
            stdout=(completed.stdout or "").strip(),
            stderr=(completed.stderr or "").strip(),
        )

    def is_git_repo(self) -> bool:
        result = self._run_git(["rev-parse", "--is-inside-work-tree"])
        return result.ok and result.stdout.lower() == "true"

    def init_repo_if_needed(self) -> None:
        if self.is_git_repo():
            return
        result = self._run_git(["init"])
        if not result.ok:
            raise RuntimeError(f"Failed to initialize git repository: {result.stderr}")

    def ensure_identity(self) -> None:
        name = self._run_git(["config", "user.name"]).stdout
        email = self._run_git(["config", "user.email"]).stdout
        if not name:
            self._run_git(["config", "user.name", "self-improver-bot"])
        if not email:
            self._run_git(["config", "user.email", "self-improver@local"])

    def ensure_initial_commit(self) -> None:
        result = self._run_git(["rev-parse", "--verify", "HEAD"])
        if result.ok:
            return
        self.ensure_identity()
        self._run_git(["add", "-A"])
        commit = self._run_git(["commit", "-m", "chore: bootstrap self improver"])
        if not commit.ok:
            status = self._run_git(["status", "--porcelain"]).stdout
            if status.strip():
                raise RuntimeError(f"Failed to create initial commit: {commit.stderr}")

    def worktree_is_clean(self) -> bool:
        status = self._run_git(["status", "--porcelain"])
        if not status.ok:
            return False
        return status.stdout.strip() == ""

    def commit_all(self, message: str) -> str:
        self.ensure_identity()
        self._run_git(["add", "-A"])
        status = self._run_git(["status", "--porcelain"])
        if not status.stdout.strip():
            return ""
        commit = self._run_git(["commit", "-m", message])
        if not commit.ok:
            raise RuntimeError(f"Commit failed: {commit.stderr}")
        sha = self._run_git(["rev-parse", "HEAD"])
        if not sha.ok:
            raise RuntimeError(f"Commit succeeded but SHA lookup failed: {sha.stderr}")
        return sha.stdout.strip()

    def build_file_tree_snapshot(self, max_files: int) -> str:
        entries: list[str] = []
        for root, dirs, files in os.walk(self.workspace):
            root_path = Path(root)
            rel_root = root_path.relative_to(self.workspace).as_posix()
            if rel_root == ".":
                rel_root = ""

            filtered_dirs = []
            for d in dirs:
                rel = f"{rel_root}/{d}".lstrip("/")
                if rel.startswith(".git") or rel.startswith(self.state_dir_name):
                    continue
                filtered_dirs.append(d)
            dirs[:] = filtered_dirs

            for file_name in files:
                rel = f"{rel_root}/{file_name}".lstrip("/")
                if rel.startswith(".git") or rel.startswith(self.state_dir_name):
                    continue
                entries.append(rel)
                if len(entries) >= max_files:
                    break
            if len(entries) >= max_files:
                break
        return "\n".join(sorted(entries))

    def read_target_files(self, target_files: list[str], max_total_bytes: int) -> str:
        blocks: list[str] = []
        budget = max_total_bytes
        for path in target_files:
            full_path = (self.workspace / path).resolve()
            if not full_path.exists() or not full_path.is_file():
                blocks.append(f"FILE: {path}\n<missing>\n")
                continue
            # Normalize path to prevent directory traversal attacks
            if ".." in path or path.startswith(".."):
                blocks.append(f"FILE: {path}\n<invalid path>\n")
                continue
            # Validate path is within workspace
            try:
                normalized_path = (self.workspace / path).resolve()
                if not normalized_path.is_relative_to(self.workspace):
                    blocks.append(f"FILE: {path}\n<outside workspace>\n")
                    continue
            except ValueError:
                blocks.append(f"FILE: {path}\n<invalid path>\n")
                continue
            content = full_path.read_text(encoding="utf-8", errors="replace")
            content = truncate_text(content, max(1_024, budget // max(1, len(target_files))))
            blocks.append(f"FILE: {path}\n{content}\n")
            budget -= len(content.encode("utf-8", errors="replace"))
            if budget <= 0:
                break
        return "\n".join(blocks)

    def status_short(self) -> str:
        result = self._run_git(["status", "--short"])
        return result.stdout if result.ok else result.stderr
