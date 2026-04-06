from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .config import RuntimeConfig
from .memory import IterationRecord, MemoryStore
from .ollama import OllamaClient, OllamaOptions
from .patcher import PatchApplier, PatchGuard
from .policy import AdaptivePolicy
from .repo import RepoManager
from .utils import extract_json_object, extract_unified_diff, truncate_text, utc_now
from .validator import ValidationReport, Validator


LOGGER = logging.getLogger("self_improver")


@dataclass
class ImprovementPlan:
    objective: str
    rationale: str
    target_files: list[str]
    validation_commands: list[str]
    success_metric: str


@dataclass
class ReviewDecision:
    approve: bool
    reason: str


@dataclass
class CycleResult:
    success: bool
    message: str
    objective: str
    score_before: float
    score_after: float
    commit_sha: str
    patch_sha256: str


class SelfImprovementSupervisor:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)

        self.repo = RepoManager(workspace=self.config.workspace, state_dir_name=self.config.state_dir.name)
        self.memory = MemoryStore(self.config.memory_db_path)
        self.policy = AdaptivePolicy.from_path(self.config.policy_path)
        self.validator = Validator(self.config.workspace, timeout_seconds=self.config.command_timeout_seconds)
        self.patch_guard = PatchGuard(
            allowed_paths=self.config.allowed_paths,
            max_patch_bytes=min(self.config.max_patch_bytes, self.policy.max_patch_bytes),
            max_patch_paths=self.config.max_patch_paths,
            max_patch_hunks=self.config.max_patch_hunks,
        )
        self.patch_applier = PatchApplier(self.config.workspace, self.config.state_dir)
        self.llm = OllamaClient(self.config.ollama_base_url, self.config.model)

    def bootstrap(self) -> None:
        if self.config.auto_init_git:
            self.repo.init_repo_if_needed()
            self.repo.ensure_initial_commit()
        if not self.config.allow_dirty_worktree and not self.repo.worktree_is_clean():
            raise RuntimeError(
                "Worktree has uncommitted changes. Commit/stash them or set allow_dirty_worktree=true."
            )
        LOGGER.info("bootstrap complete")

    def run_forever(self, max_cycles: int | None = None) -> None:
        cycle = 0
        consecutive_failures = 0
        while max_cycles is None or cycle < max_cycles:
            cycle += 1
            LOGGER.info("starting cycle %d", cycle)
            result = self.run_cycle()
            if result.success:
                consecutive_failures = 0
                LOGGER.info("cycle %d succeeded: %s", cycle, result.message)
            else:
                consecutive_failures += 1
                LOGGER.warning("cycle %d failed: %s", cycle, result.message)

            self.policy.save(self.config.policy_path)

            if consecutive_failures >= self.config.max_consecutive_failures_before_cooldown:
                LOGGER.warning(
                    "cooldown triggered after %d failures; sleeping for %ds",
                    consecutive_failures,
                    self.config.cooldown_seconds,
                )
                time.sleep(self.config.cooldown_seconds)
                consecutive_failures = 0

            if max_cycles is None or cycle < max_cycles:
                time.sleep(self.config.cycle_sleep_seconds)

    def run_cycle(self) -> CycleResult:
        started_at = utc_now()
        objective = "unplanned"
        commit_sha = ""
        patch_sha256 = ""
        score_before = 0.0
        score_after = 0.0

        try:
            if not self.config.allow_dirty_worktree and not self.repo.worktree_is_clean():
                raise RuntimeError("Worktree is dirty; refusing autonomous edits without allow_dirty_worktree=true.")

            baseline_report = self.validator.run(self.config.validate_commands)
            score_before = baseline_report.score

            plan = self._plan_next_iteration(baseline_report)
            objective = plan.objective

            patch_text = self._generate_patch(plan)
            patch_validation = self.patch_guard.validate(patch_text)
            patch_sha256 = patch_validation.patch_sha256
            if not patch_validation.ok:
                raise RuntimeError(patch_validation.message)

            review = self._review_patch(plan, patch_text, patch_validation.changed_paths)
            if not review.approve:
                raise RuntimeError(f"supervisor rejected patch: {review.reason}")

            applied, apply_error = self.patch_applier.apply(patch_text)
            if not applied:
                raise RuntimeError(f"patch apply failed: {apply_error}")

            commands = plan.validation_commands or self.config.validate_commands
            post_report = self.validator.run(commands)
            score_after = post_report.score

            accepted = self._should_accept_change(baseline_report, post_report)
            if not accepted:
                rollback_ok, rollback_error = self.patch_applier.rollback_last_patch()
                if not rollback_ok:
                    raise RuntimeError(
                        f"validation failed and rollback failed. manual repair required: {rollback_error}"
                    )
                raise RuntimeError("validation regression detected")

            commit_sha = self.repo.commit_all(self._build_commit_message(plan.objective))
            if not commit_sha:
                raise RuntimeError("patch applied but produced no commit")

            self._record_success(
                started_at=started_at,
                objective=objective,
                score_before=score_before,
                score_after=score_after,
                plan=plan,
                patch_sha256=patch_sha256,
                commit_sha=commit_sha,
                validation_report=post_report,
            )
            return CycleResult(
                success=True,
                message=f"commit {commit_sha[:12]}",
                objective=objective,
                score_before=score_before,
                score_after=score_after,
                commit_sha=commit_sha,
                patch_sha256=patch_sha256,
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self._record_failure(
                started_at=started_at,
                objective=objective,
                score_before=score_before,
                score_after=score_after,
                patch_sha256=patch_sha256,
                commit_sha=commit_sha,
                error_message=message,
            )
            self.policy.update_after_iteration(success=False, score_delta=score_after - score_before)
            return CycleResult(
                success=False,
                message=message,
                objective=objective,
                score_before=score_before,
                score_after=score_after,
                commit_sha=commit_sha,
                patch_sha256=patch_sha256,
            )

    def _record_success(
        self,
        *,
        started_at: str,
        objective: str,
        score_before: float,
        score_after: float,
        plan: ImprovementPlan,
        patch_sha256: str,
        commit_sha: str,
        validation_report: ValidationReport,
    ) -> None:
        finished_at = utc_now()
        record = IterationRecord(
            started_at=started_at,
            finished_at=finished_at,
            objective=objective,
            success=True,
            score_before=score_before,
            score_after=score_after,
            plan_json={
                "objective": plan.objective,
                "rationale": plan.rationale,
                "target_files": plan.target_files,
                "validation_commands": plan.validation_commands,
                "success_metric": plan.success_metric,
            },
            validation_json=validation_report.to_json(),
            patch_sha256=patch_sha256,
            commit_sha=commit_sha,
            error_message="",
        )
        iteration_id = self.memory.record_iteration(record)
        lesson = self._derive_lesson(plan, validation_report)
        self.memory.record_lesson(iteration_id, lesson)
        self.policy.update_after_iteration(success=True, score_delta=score_after - score_before)

    def _record_failure(
        self,
        *,
        started_at: str,
        objective: str,
        score_before: float,
        score_after: float,
        patch_sha256: str,
        commit_sha: str,
        error_message: str,
    ) -> None:
        finished_at = utc_now()
        record = IterationRecord(
            started_at=started_at,
            finished_at=finished_at,
            objective=objective,
            success=False,
            score_before=score_before,
            score_after=score_after,
            plan_json={},
            validation_json={},
            patch_sha256=patch_sha256,
            commit_sha=commit_sha,
            error_message=error_message,
        )
        self.memory.record_iteration(record)

    @staticmethod
    def _build_commit_message(objective: str) -> str:
        objective = objective.strip().replace("\n", " ")
        if len(objective) > 60:
            objective = objective[:60].rstrip() + "..."
        return f"bot: {objective}"

    @staticmethod
    def _should_accept_change(before: ValidationReport, after: ValidationReport) -> bool:
        if before.passed and not after.passed:
            return False
        return after.score >= before.score

    def _plan_next_iteration(self, baseline_report: ValidationReport) -> ImprovementPlan:
        file_tree = self.repo.build_file_tree_snapshot(self.config.planner_context_files)
        iterations = self.memory.recent_iteration_summary(limit=12)
        lessons = self.memory.recent_lessons(limit=10)

        lessons_text = "\n".join(f"- {line}" for line in lessons) if lessons else "- none"
        baseline_json = json.dumps(baseline_report.to_json(), ensure_ascii=True, indent=2)
        context = truncate_text(
            f"FILE TREE:\n{file_tree}\n\nRECENT ITERATIONS:\n{iterations}\n\nLESSONS:\n{lessons_text}\n\nBASELINE:\n{baseline_json}",
            self.config.planner_context_bytes,
        )

        system_prompt = (
            "You are a software-improvement planner. "
            "Focus on reliability and autonomous operation improvements. "
            "Return strict JSON only."
        )
        user_prompt = (
            "Create ONE safe incremental improvement plan.\n"
            "Constraints:\n"
            "- Must improve the self_improver codebase itself.\n"
            "- Must be testable in one patch.\n"
            "- Keep scope small (<= 8 files).\n"
            "- Avoid risky or broad refactors.\n\n"
            "JSON schema:\n"
            "{\n"
            '  "objective": "string",\n'
            '  "rationale": "string",\n'
            '  "target_files": ["relative/path.py"],\n'
            '  "validation_commands": ["python -m pytest -q"],\n'
            '  "success_metric": "string"\n'
            "}\n\n"
            f"Repository context:\n{context}"
        )

        text = self.llm.generate(
            prompt=user_prompt,
            system=system_prompt,
            options=OllamaOptions(
                temperature=self.policy.planner_temperature,
                num_predict=min(self.policy.num_predict, 2200),
            ),
        )
        payload = extract_json_object(text)

        objective = str(payload.get("objective", "")).strip()
        rationale = str(payload.get("rationale", "")).strip()
        success_metric = str(payload.get("success_metric", "")).strip()
        target_files = payload.get("target_files", [])
        validation_commands = payload.get("validation_commands", [])

        if not isinstance(target_files, list):
            target_files = []
        if not isinstance(validation_commands, list):
            validation_commands = []

        normalized_targets = []
        for item in target_files:
            if isinstance(item, str) and item.strip():
                normalized_targets.append(item.replace("\\", "/").lstrip("./"))
        normalized_commands = []
        for cmd in validation_commands:
            if isinstance(cmd, str) and cmd.strip():
                normalized_commands.append(cmd.strip())

        if not objective:
            objective = "Improve supervisor reliability with stronger tests"
        if not rationale:
            rationale = "Fallback rationale because planner response was incomplete."
        if not success_metric:
            success_metric = "Validation score must not regress."

        return ImprovementPlan(
            objective=objective,
            rationale=rationale,
            target_files=normalized_targets,
            validation_commands=normalized_commands,
            success_metric=success_metric,
        )

    def _generate_patch(self, plan: ImprovementPlan) -> str:
        file_context = self.repo.read_target_files(plan.target_files, self.config.target_file_context_bytes)
        summary = self.memory.recent_iteration_summary(limit=8)

        system_prompt = (
            "You write minimal, correct unified git patches for Python projects. "
            "Return only a patch and no prose."
        )
        user_prompt = (
            f"Objective: {plan.objective}\n"
            f"Rationale: {plan.rationale}\n"
            f"Success metric: {plan.success_metric}\n"
            f"Allowed paths: {', '.join(self.config.allowed_paths)}\n"
            "Hard constraints:\n"
            "- Output MUST be a valid unified diff patch.\n"
            "- Edit only allowed paths.\n"
            "- Keep patch concise.\n"
            "- Add or update tests when behavior changes.\n"
            "- Do not include markdown fences.\n\n"
            f"Recent iteration summary:\n{summary}\n\n"
            f"Current target files:\n{file_context}\n"
        )

        raw = self.llm.generate(
            prompt=user_prompt,
            system=system_prompt,
            options=OllamaOptions(
                temperature=self.policy.coder_temperature,
                num_predict=self.policy.num_predict,
            ),
        )
        return extract_unified_diff(raw)

    def _review_patch(self, plan: ImprovementPlan, patch_text: str, changed_paths: list[str]) -> ReviewDecision:
        system_prompt = (
            "You are a strict patch reviewer. Return JSON only with approve bool and reason string."
        )
        user_prompt = (
            f"Objective: {plan.objective}\n"
            f"Changed paths: {json.dumps(changed_paths)}\n"
            "Review criteria:\n"
            "- reject if patch likely breaks tests or ignores objective\n"
            "- reject if patch appears malformed\n"
            "- approve if patch is coherent and low risk\n\n"
            "JSON schema:\n"
            '{"approve": true, "reason": "short reason"}\n\n'
            "Patch:\n"
            f"{patch_text}"
        )
        try:
            text = self.llm.generate(
                prompt=user_prompt,
                system=system_prompt,
                options=OllamaOptions(
                    temperature=self.policy.reviewer_temperature,
                    num_predict=500,
                ),
            )
            payload = extract_json_object(text)
            approve = bool(payload.get("approve", False))
            reason = str(payload.get("reason", "")).strip() or "no reason"
            return ReviewDecision(approve=approve, reason=reason)
        except Exception:  # noqa: BLE001
            # Deterministic gates still run, so review failures are not fatal.
            return ReviewDecision(approve=True, reason="review parse failed; fallback to deterministic gates")

    def _derive_lesson(self, plan: ImprovementPlan, validation_report: ValidationReport) -> str:
        if validation_report.passed:
            return f"Successful objective '{plan.objective}' with validation score {validation_report.score:.1f}."
        return f"Objective '{plan.objective}' reduced reliability; keep patches smaller and add focused tests."

    def status(self) -> dict:
        return {
            "config": {
                "workspace": str(self.config.workspace),
                "model": self.config.model,
                "ollama_base_url": self.config.ollama_base_url,
            },
            "memory": self.memory.stats(),
            "policy": json.loads(self.policy.to_json()),
            "git_status": self.repo.status_short(),
        }
