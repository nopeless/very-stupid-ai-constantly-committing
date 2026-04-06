from __future__ import annotations

import hashlib
import subprocess
import time
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
    ) -> None:
        self.allowed_paths = [self._normalize_path(item) for item in allowed_paths]
        self.max_patch_bytes = max_patch_bytes
        self.max_patch_paths = max_patch_paths
        self.max_patch_hunks = max_patch_hunks

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
            elif line.startswith("+++ b/"):
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

    def _run_git_apply(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "apply", *args],
            cwd=str(self.workspace),
            check=False,
            capture_output=True,
            text=True,
        )

    def _write_patch_file(self, diff_text: str) -> None:
        # git apply expects LF-delimited patch content.
        self.last_patch_path.write_text(diff_text, encoding="utf-8", newline="\n")

    def apply(self, diff_text: str, max_retries: int = 3, base_delay: float = 0.1) -> tuple[bool, str]:
        if not diff_text or not isinstance(diff_text, str):
            return False, "Patch text must be a non-empty string."
        if len(diff_text) > 1_048_576:
            return False, "Patch text exceeds maximum allowed size (1MB)."

        # Validate patch paths against allowlist before applying
        validation = self.guard.validate(diff_text)
        if not validation.ok:
            return False, validation.message

        self._write_patch_file(diff_text)
        result: subprocess.CompletedProcess[str] | None = None
        for attempt in range(max_retries):
            result = self._run_git_apply(["--index", "--whitespace=nowarn", str(self.last_patch_path)])
            if result.returncode == 0:
                return True, ""
            if attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))
            if result.returncode == -1 or result.returncode == 124:
                return False, f"Patch apply timed out on attempt {attempt + 1}."
        assert result is not None
        return False, (result.stderr or result.stdout or "git apply failed").strip()

    def check(self, diff_text: str) -> tuple[bool, str]:
        if not diff_text or not isinstance(diff_text, str):
            return False, "Patch text must be a non-empty string."
        if len(diff_text) > 1_048_576:
            return False, "Patch text exceeds maximum allowed size (1MB)."

        self._write_patch_file(diff_text)
        result = self._run_git_apply(["--check", "--index", "--whitespace=nowarn", str(self.last_patch_path)])
        if result.returncode == 0:
            return True, ""
        if result.returncode == -1 or result.returncode == 124:
            return False, "Patch check timed out."
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
        if result.returncode == -1 or result.returncode == 124 or fallback.returncode == -1 or fallback.returncode == 124:
            message = "Rollback timed out."
        return False, message.strip()
