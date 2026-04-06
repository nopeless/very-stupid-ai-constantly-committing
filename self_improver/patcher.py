from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass
class PatchValidation:
    ok: bool
    message: str
    changed_paths: list[str]
    patch_sha256: str


class PatchGuard:
    def __init__(
        self,
        *,
        allowed_paths: list[str],
        max_patch_bytes: int,
        max_patch_paths: int,
        max_patch_hunks: int,
        max_repeated_objectives: int = 3,
    ) -> None:
        self.allowed_paths = [self._normalize_path(item) for item in allowed_paths]
        self.max_patch_bytes = max_patch_bytes
        self.max_patch_paths = max_patch_paths
        self.max_patch_hunks = max_patch_hunks
        self.max_repeated_objectives = max_repeated_objectives

    @staticmethod
    def _normalize_path(path: str) -> str:
        normalized = path.replace("\\", "/").strip().lstrip("./")
        return str(PurePosixPath(normalized))

    def _is_allowed(self, path: str) -> bool:
        for entry in self.allowed_paths:
            if path == entry:
                return True
            if path.startswith(entry + "/"):
                return True
        return False

    @staticmethod
    def _extract_changed_paths(diff_text: str) -> list[str]:
        paths: set[str] = set()
        for line in diff_text.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                if len(parts) >= 4:
                    right = parts[3]
                    if right.startswith("b/"):
                        right = right[2:]
                    if right != "/dev/null":
                        paths.add(right)
            elif line.startswith("+\+\+ b/"):
                path = line[6:].strip()
                if path != "/dev/null":
                    paths.add(path)
        return sorted(paths)

    def validate(self, diff_text: str) -> PatchValidation:
        if not diff_text or not isinstance(diff_text, str):
            return PatchValidation(
                ok=False,
                message="Patch text must be a non-empty string.",
                changed_paths=[],
                patch_sha256="",
            )
        digest = hashlib.sha256(diff_text.encode("utf-8", errors="replace")).hexdigest()
        size = len(diff_text.encode("utf-8", errors="replace"))
        if size > self.max_patch_bytes:
            return PatchValidation(
                ok=False,
                message=f"Patch too large ({size} bytes > {self.max_patch_bytes} bytes).",
                changed_paths=[],
                patch_sha256=digest,
            )
        hunk_count = sum(1 for line in diff_text.splitlines() if line.startswith("@@"))
        if hunk_count > self.max_patch_hunks:
            return PatchValidation(
                ok=False,
                message=f"Patch has too many hunks ({hunk_count} > {self.max_patch_hunks}).",
                changed_paths=[],
                patch_sha256=digest,
            )
        changed = [self._normalize_path(p) for p in self._extract_changed_paths(diff_text)]
        if not changed:
            return PatchValidation(
                ok=False,
                message="Patch does not include any changed paths.",
                changed_paths=[],
                patch_sha256=digest,
            )
        if len(changed) > self.max_patch_paths:
            return PatchValidation(
                ok=False,
                message=f"Patch touches too many files ({len(changed)} > {self.max_patch_paths}).",
                changed_paths=changed,
                patch_sha256=digest,
            )
        for path in changed:
            if path.startswith("../") or path.startswith("/") or "/../" in path:
                return PatchValidation(
                    ok=False,
                    message=f"Path traversal is not allowed: {path}",
                    changed_paths=changed,
                    patch_sha256=digest,
                )
            if not self._is_allowed(path):
                return PatchValidation(
                    ok=False,
                    message=f"Path is outside allowlist: {path}",
                    changed_paths=changed,
                    patch_sha256=digest,
                )
        return PatchValidation(ok=True, message="ok", changed_paths=changed, patch_sha256=digest)


class PatchApplier:
    def __init__(self, workspace: Path, state_dir: Path) -> None:
        self.workspace = workspace
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.last_patch_path = self.state_dir / "last.patch"
        self.objective_history: list[str] = []

    def _run_git_apply(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "apply", *args],
            cwd=str(self.workspace),
            check=False,
            capture_output=True,
            text=True,
        )

    def apply(self, diff_text: str) -> tuple[bool, str]:
        if not diff_text or not isinstance(diff_text, str):
            return False, "Patch text must be a non-empty string."
        if len(diff_text) > 1024 * 1024:
            return False, "Patch text exceeds maximum allowed size (1MB)."
        self.last_patch_path.write_text(diff_text, encoding="utf-8", newline="\n")
        result = self._run_git_apply(["--index", "--whitespace=nowarn", str(self.last_patch_path)])
        if result.returncode == 0:
            return True, ""
        return False, (result.stderr or result.stdout or "git apply failed").strip()

    def check(self, diff_text: str) -> tuple[bool, str]:
        if not diff_text or not isinstance(diff_text, str):
            return False, "Patch text must be a non-empty string."
        if len(diff_text) > 1024 * 1024:
            return False, "Patch text exceeds maximum allowed size (1MB)."
        self.last_patch_path.write_text(diff_text, encoding="utf-8", newline="\n")
        result = self._run_git_apply(["--check", "--index", "--whitespace=nowarn", str(self.last_patch_path)])
        if result.returncode == 0:
            return True, ""
        return False, (result.stderr or result.stdout or "git apply --check failed").strip()

    def rollback_last_patch(self) -> tuple[bool, str]:
        if not self.last_patch_path.exists():
            return True, ""
        result = self._run_git_apply(["-R", "--index", "--whitespace=nowarn", str(self.last_patch_path)])
        if result.returncode == 0:
            return True, ""
        fallback = self._run_git_apply(["-R", "--whitespace=nowarn", str(self.last_patch_path)])
        if fallback.returncode == 0:
            return True, ""
        message = fallback.stderr or fallback.stdout or result.stderr or "rollback failed"
        return False, message.strip()

    def _extract_objective_from_diff(self, diff_text: str) -> str:
        """Extract a high-level objective description from the diff."""
        lines = diff_text.splitlines()
        for line in lines:
            if line.startswith("+" + "=" * 20) or line.startswith("+" + "#"):
                # Look for comments or headers that might describe the objective
                if "objective" in line.lower() or "goal" in line.lower() or "purpose" in line.lower():
                    return line.strip()
        # Fallback: use first non-empty line that looks like a description
        for line in lines:
            if line.strip() and not line.startswith("diff") and not line.startswith("@@") and not line.startswith("+") and not line.startswith("-"):
                return line.strip()
        return ""

    def _is_objective_repeated(self, objective: str) -> bool:
        """Check if the objective has been repeated too many times recently."""
        objective_lower = objective.lower()
        recent_objectives = self.objective_history[-self.max_repeated_objectives:]
        for recent in recent_objectives:
            if objective_lower in recent.lower() or recent.lower() in objective_lower:
                return True
        return False

    def apply_with_diversity_check(self, diff_text: str) -> tuple[bool, str]:
        """Apply patch but reject if objective is too similar to recent ones."""
        objective = self._extract_objective_from_diff(diff_text)
        if objective and self._is_objective_repeated(objective):
            return False, f"Objective appears too similar to recent objectives: {objective}"
        success, message = self.apply(diff_text)
        if success and objective:
            self.objective_history.append(objective)
        return success, message
