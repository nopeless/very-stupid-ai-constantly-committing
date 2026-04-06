from __future__ import annotations

import difflib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

from .config import RuntimeConfig
from .memory import IterationRecord, MemoryStore
from .ollama import OllamaClient, OllamaOptions
from .patcher import PatchApplier, PatchGuard
from .policy import AdaptivePolicy
from .repo import RepoManager
from .todo import TodoEntry, TodoQueue
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
    todo_text: str = ""


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
        self.todo_queue = TodoQueue(self.config.todo_path)
        self.llm = OllamaClient(self.config.ollama_base_url, self.config.model)

    def bootstrap(self) -> None:
        if self.config.auto_init_git:
            self.repo.init_repo_if_needed()
            self.repo.ensure_initial_commit()
        dirty_sha = self._checkpoint_dirty_worktree("bootstrap")
        if dirty_sha:
            LOGGER.warning("dirty worktree checkpointed at bootstrap: %s", dirty_sha[:12])
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
        active_todo: TodoEntry | None = None
        plan: ImprovementPlan | None = None
        baseline_json: dict = {}
        post_json: dict = {}

        # Initialize duplicate tracking
        self._duplicate_objective_cache = set()
        started_at = utc_now()
        objective = "unplanned"
        commit_sha = ""
        patch_sha256 = ""
        score_before = 0.0
        score_after = 0.0
        active_todo: TodoEntry | None = None
        plan: ImprovementPlan | None = None
        baseline_json: dict = {}
        post_json: dict = {}

        try:
            dirty_sha = self._checkpoint_dirty_worktree("cycle-start")
            if dirty_sha:
                LOGGER.warning("dirty worktree checkpointed at cycle start: %s", dirty_sha[:12])

            ollama_ok, ollama_message = self.llm.health_check(
                timeout_seconds=self.config.ollama_healthcheck_timeout_seconds
            )
            if not ollama_ok:
                raise RuntimeError(f"ollama health check failed: {ollama_message}")

            active_todo = self._next_todo_entry()
            if active_todo is not None:
                LOGGER.info("active TODO: %s", active_todo.text)
                # Track completion rate by ensuring all TODOs are resolved
                self._track_todo_completion(active_todo)
            else:
                LOGGER.info("no active TODO found")

            # Periodically review completed items to prevent duplicate work
            self._review_completed_items()

            # Prevent duplicate objectives by checking for similar objectives
            if active_todo is not None:
                if self._is_duplicate_objective(objective, active_todo.text):
                    LOGGER.warning("duplicate objective detected: %s", active_todo.text)
                    active_todo = None

            baseline_report = self.validator.run(self.config.validate_commands)
            baseline_json = baseline_report.to_json()
            score_before = baseline_report.score

            plan = self._plan_next_iteration(baseline_report, forced_todo=active_todo)
            objective = plan.objective

            patch_text = ""
            selected_paths: list[str] = []
            last_patch_error = ""
            for _ in range(3):
                candidate_patch = self._generate_patch(plan)
                patch_validation = self.patch_guard.validate(candidate_patch)
                patch_sha256 = patch_validation.patch_sha256
                if not patch_validation.ok:
                    last_patch_error = patch_validation.message
                    continue
                if not self._paths_within_targets(patch_validation.changed_paths, plan.target_files):
                    last_patch_error = "patch touched files outside planner target_files"
                    continue

                review = self._review_patch(plan, candidate_patch, patch_validation.changed_paths)
                if not review.approve:
                    if self._is_hard_reject_reason(review.reason):
                        last_patch_error = f"supervisor rejected patch: {review.reason}"
                        continue
                    LOGGER.warning("review soft-reject ignored: %s", review.reason)

                patch_check_ok, patch_check_error = self.patch_applier.check(candidate_patch)
                if not patch_check_ok:
                    last_patch_error = f"patch check failed: {patch_check_error}"
                    continue

                patch_text = candidate_patch
                selected_paths = patch_validation.changed_paths
                break

            if not patch_text:
                raise RuntimeError(f"failed to generate a valid patch: {last_patch_error}")

            applied, apply_error = self.patch_applier.apply(patch_text)
            if not applied:
                raise RuntimeError(f"patch apply failed: {apply_error}")

            commands = plan.validation_commands or self.config.validate_commands
            post_report = self.validator.run(commands)
            post_json = post_report.to_json()
            score_after = post_report.score

            accepted = self._should_accept_change(baseline_report, post_report)
            if not accepted:
                rollback_ok, rollback_error = self.patch_applier.rollback_last_patch()
                if not rollback_ok:
                    raise RuntimeError(
                        f"validation failed and rollback failed. manual repair required: {rollback_error}"
                    )
                raise RuntimeError("validation regression detected")

            if active_todo is not None:
                if self._todo_resolved(active_todo, plan, selected_paths, post_report):
                    removed = self.todo_queue.remove_entry(active_todo)
                    if removed:
                        LOGGER.info("resolved TODO and removed top entry: %s", active_todo.text)
                    else:
                        LOGGER.warning("TODO changed during cycle; skipping removal: %s", active_todo.text)
                else:
                    LOGGER.info("TODO kept in queue (not resolved): %s", active_todo.text)

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
            self._write_cycle_artifact(
                started_at=started_at,
                objective=objective,
                success=True,
                message=f"commit {commit_sha[:12]}",
                todo_text=active_todo.text if active_todo is not None else "",
                plan=plan,
                patch_sha256=patch_sha256,
                commit_sha=commit_sha,
                score_before=score_before,
                score_after=score_after,
                baseline_validation=baseline_json,
                post_validation=post_json,
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
            self._write_cycle_artifact(
                started_at=started_at,
                objective=objective,
                success=False,
                message=message,
                todo_text=active_todo.text if active_todo is not None else "",
                plan=plan,
                patch_sha256=patch_sha256,
                commit_sha=commit_sha,
                score_before=score_before,
                score_after=score_after,
                baseline_validation=baseline_json,
                post_validation=post_json,
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
                "todo_text": plan.todo_text,
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

    def _checkpoint_dirty_worktree(self, phase: str) -> str:
        if self.repo.worktree_is_clean():
            return ""

        if self.config.auto_commit_dirty_worktree:
            commit_message = f"bot: dirty checkpoint ({phase})"
            checkpoint_sha = self.repo.commit_all(commit_message)
            if checkpoint_sha:
                return checkpoint_sha
            if self.repo.worktree_is_clean():
                return ""
            raise RuntimeError("Dirty worktree detected and automatic dirty checkpoint commit failed.")

        if self.config.allow_dirty_worktree:
            return ""

        raise RuntimeError(
            "Worktree has uncommitted changes. Enable auto_commit_dirty_worktree or set allow_dirty_worktree=true."
        )

    def _write_cycle_artifact(
        self,
        *,
        started_at: str,
        objective: str,
        success: bool,
        message: str,
        todo_text: str,
        plan: ImprovementPlan | None,
        patch_sha256: str,
        commit_sha: str,
        score_before: float,
        score_after: float,
        baseline_validation: dict,
        post_validation: dict,
    ) -> None:
        finished_at = utc_now()
        payload = {
            "started_at": started_at,
            "finished_at": finished_at,
            "success": success,
            "message": message,
            "objective": objective,
            "todo_text": todo_text,
            "plan": {
                "objective": plan.objective,
                "rationale": plan.rationale,
                "target_files": plan.target_files,
                "validation_commands": plan.validation_commands,
                "success_metric": plan.success_metric,
                "todo_text": plan.todo_text,
            }
            if plan is not None
            else {},
            "patch_sha256": patch_sha256,
            "commit_sha": commit_sha,
            "score_before": score_before,
            "score_after": score_after,
            "baseline_validation": baseline_validation,
            "post_validation": post_validation,
        }

        cycles_dir = self.config.logs_dir / "cycles"
        cycles_dir.mkdir(parents=True, exist_ok=True)
        safe_timestamp = finished_at.replace(":", "-")
        artifact_path = cycles_dir / f"{safe_timestamp}.json"
        artifact_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

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

    def _plan_next_iteration(
        self,
        baseline_report: ValidationReport,
        forced_todo: TodoEntry | None = None,
    ) -> ImprovementPlan:
        file_tree = self.repo.build_file_tree_snapshot(self.config.planner_context_files)
        iterations = self.memory.recent_iteration_summary(limit=12)
        recent_objectives = self.memory.recent_objectives(limit=12)
        lessons = self.memory.recent_lessons(limit=10)
        briefing = self.memory.development_briefing(window=24)

        lessons_text = "\n".join(f"- {line}" for line in lessons) if lessons else "- none"
        objectives_text = "\n".join(f"- {line}" for line in recent_objectives) if recent_objectives else "- none"
        baseline_json = json.dumps(baseline_report.to_json(), ensure_ascii=True, indent=2)
        context = truncate_text(
            "FILE TREE:\n"
            f"{file_tree}\n\n"
            f"RECENT ITERATIONS:\n{iterations}\n\n"
            f"RECENT OBJECTIVES:\n{objectives_text}\n\n"
            f"LESSONS:\n{lessons_text}\n\n"
            f"SESSION BRIEFING:\n{briefing}\n\n"
            f"BASELINE:\n{baseline_json}",
            self.config.planner_context_bytes,
        )
        todo_requirement = ""
        if forced_todo is not None:
            todo_requirement = (
                "Top TODO entry to resolve this cycle:\n"
                f"- {forced_todo.text}\n\n"
                "Your plan must resolve this TODO item directly.\n"
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
            f"{todo_requirement}"
            f"Repository context:\n{context}"
        )

        payload = {}
        last_error = ""
        for _ in range(3):
            try:
                text = self.llm.generate(
                    prompt=user_prompt,
                    system=system_prompt,
                    options=OllamaOptions(
                        temperature=self.policy.planner_temperature,
                        num_predict=min(self.policy.num_predict, 2200),
                    ),
                    json_mode=True,
                )
                payload = extract_json_object(text)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
        if not payload:
            LOGGER.warning("planner fallback activated: %s", last_error)
            payload = {
                "objective": "Increase robustness of prompt parsing and retry logic",
                "rationale": "Planner output was malformed; improve resilience for perpetual cycles.",
                "target_files": ["self_improver/supervisor.py", "self_improver/ollama.py", "tests/test_utils.py"],
                "validation_commands": list(self.config.validate_commands),
                "success_metric": "Cycle should survive malformed model output.",
            }

        objective = str(payload.get("objective", "")).strip()
        rationale = str(payload.get("rationale", "")).strip()
        success_metric = str(payload.get("success_metric", "")).strip()
        target_files = payload.get("target_files", [])
        validation_commands = payload.get("validation_commands", [])

        if not isinstance(target_files, list):
            target_files = []
        if not isinstance(validation_commands, list):
            validation_commands = []

        candidate_targets: list[str] = []
        for item in target_files:
            if isinstance(item, str) and item.strip():
                candidate_targets.append(item.replace("\\", "/").lstrip("./"))
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

        todo_text = ""
        if forced_todo is not None:
            todo_text = forced_todo.text
            objective = f"Resolve TODO: {forced_todo.text}"
            if not candidate_targets:
                candidate_targets = self._extract_file_hints_from_text(forced_todo.text)
        else:
            duplicate_count = sum(
                1 for item in recent_objectives if item.strip().lower() == objective.strip().lower()
            )
            if duplicate_count >= 2:
                objective = "Diversify planning logic to avoid repeated objectives"
                rationale = "The same objective repeated in recent cycles; forcing planning diversity."
                candidate_targets = ["self_improver/supervisor.py", "tests/test_memory.py"]
                normalized_commands = list(self.config.validate_commands)
                success_metric = "Consecutive cycles should avoid duplicate objectives."

        normalized_targets = self._sanitize_target_files(candidate_targets)
        if not normalized_targets:
            normalized_targets = self._default_target_files()

        return ImprovementPlan(
            objective=objective,
            rationale=rationale,
            target_files=normalized_targets,
            validation_commands=normalized_commands,
            success_metric=success_metric,
            todo_text=todo_text,
        )

    def _generate_patch(self, plan: ImprovementPlan) -> str:
        try:
            return self._generate_patch_from_replacements(plan)
        except Exception as repl_exc:  # noqa: BLE001
            LOGGER.warning("replacement generation failed, trying file-block mode: %s", repl_exc)
            try:
                return self._generate_patch_from_file_blocks(plan)
            except Exception as block_exc:  # noqa: BLE001
                LOGGER.warning("file-block generation failed, trying structured JSON mode: %s", block_exc)
                try:
                    return self._generate_patch_from_structured_edits(plan)
                except Exception as json_exc:  # noqa: BLE001
                    LOGGER.warning("structured edit generation failed, falling back to raw diff mode: %s", json_exc)
                    return self._generate_patch_from_raw_diff(plan)

    def _generate_patch_from_replacements(self, plan: ImprovementPlan) -> str:
        if not plan.target_files:
            raise RuntimeError("replacement mode requires target_files from planner")

        file_context = self.repo.read_target_files(plan.target_files, self.config.target_file_context_bytes)
        target_list = [self._normalize_rel_path(path) for path in plan.target_files]

        system_prompt = "You return strict JSON only."
        user_prompt = (
            f"Objective: {plan.objective}\n"
            f"Rationale: {plan.rationale}\n"
            "Return JSON with this exact shape:\n"
            "{\n"
            '  "replacements": [\n'
            '    {"path": "relative/path.py", "find": "exact old snippet", "replace": "new snippet"}\n'
            "  ]\n"
            "}\n\n"
            "Rules:\n"
            "- path must be one of target_files\n"
            "- keep each find snippet short and exact\n"
            "- use \\n for newlines inside snippets\n"
            "- include only replacements necessary for the objective\n"
            "- no markdown or prose\n\n"
            f"target_files={json.dumps(target_list)}\n\n"
            f"Current files:\n{file_context}"
        )
        raw = self.llm.generate(
            prompt=user_prompt,
            system=system_prompt,
            options=OllamaOptions(
                temperature=self.policy.coder_temperature,
                num_predict=min(self.policy.num_predict, 1800),
            ),
            json_mode=True,
        )
        payload = extract_json_object(raw)
        replacements = payload.get("replacements")
        if not isinstance(replacements, list) or not replacements:
            raise RuntimeError("replacement response did not include any replacements")

        updated_content: dict[str, str] = {}
        for rel_path in target_list:
            file_path = (self.config.workspace / rel_path).resolve()
            if not file_path.exists() or not file_path.is_file():
                continue
            updated_content[rel_path] = file_path.read_text(encoding="utf-8", errors="replace")

        changed_paths: set[str] = set()
        for item in replacements:
            if not isinstance(item, dict):
                continue
            path_raw = item.get("path", "")
            find_raw = item.get("find", "")
            replace_raw = item.get("replace", "")
            if not isinstance(path_raw, str) or not isinstance(find_raw, str) or not isinstance(replace_raw, str):
                continue
            rel_path = self._normalize_rel_path(path_raw)
            if rel_path not in updated_content:
                continue

            source = updated_content[rel_path]
            if find_raw not in source:
                continue
            updated_content[rel_path] = source.replace(find_raw, replace_raw, 1)
            changed_paths.add(rel_path)

        if not changed_paths:
            raise RuntimeError("replacement edits did not match current files")

        edits = [(path, updated_content[path]) for path in sorted(changed_paths)]
        return self._build_patch_from_content_edits(edits)

    def _generate_patch_from_raw_diff(self, plan: ImprovementPlan) -> str:
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

        last_error = ""
        for _ in range(3):
            try:
                raw = self.llm.generate(
                    prompt=user_prompt,
                    system=system_prompt,
                    options=OllamaOptions(
                        temperature=self.policy.coder_temperature,
                        num_predict=self.policy.num_predict,
                    ),
                )
                return extract_unified_diff(raw)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
        raise RuntimeError(f"coder failed to produce unified diff after retries: {last_error}")

    def _generate_patch_from_structured_edits(self, plan: ImprovementPlan) -> str:
        if not plan.target_files:
            raise RuntimeError("structured edit mode requires target_files from planner")

        file_context = self.repo.read_target_files(plan.target_files, self.config.target_file_context_bytes)
        target_list = [self._normalize_rel_path(path) for path in plan.target_files]

        system_prompt = "You return strict JSON only."
        user_prompt = (
            f"Objective: {plan.objective}\n"
            f"Rationale: {plan.rationale}\n"
            "Return JSON with this exact shape:\n"
            "{\n"
            '  "edits": [\n'
            '    {"path": "relative/path.py", "content": "FULL FILE CONTENT"}\n'
            "  ]\n"
            "}\n\n"
            "Rules:\n"
            "- edits must be a subset of target_files\n"
            "- content must be full file contents after your changes\n"
            "- only include files you actually changed\n"
            "- do not include markdown fences or commentary\n\n"
            f"target_files={json.dumps(target_list)}\n\n"
            f"Current files:\n{file_context}"
        )
        raw = self.llm.generate(
            prompt=user_prompt,
            system=system_prompt,
            options=OllamaOptions(
                temperature=self.policy.coder_temperature,
                num_predict=self.policy.num_predict,
            ),
            json_mode=True,
        )
        payload = extract_json_object(raw)
        edits = payload.get("edits")
        if not isinstance(edits, list) or not edits:
            raise RuntimeError("structured edit response did not include any edits")

        edits_to_apply: list[tuple[str, str]] = []
        for item in edits:
            if not isinstance(item, dict):
                continue
            path_raw = item.get("path", "")
            content_raw = item.get("content", "")
            if not isinstance(path_raw, str) or not isinstance(content_raw, str):
                continue
            rel_path = self._normalize_rel_path(path_raw)
            if rel_path not in target_list:
                continue

            file_path = (self.config.workspace / rel_path).resolve()
            if not file_path.exists() or not file_path.is_file():
                continue

            new_text = content_raw.replace("\r\n", "\n")
            edits_to_apply.append((rel_path, new_text))

        if not edits_to_apply:
            raise RuntimeError("structured edit response did not produce usable file diffs")
        return self._build_patch_from_content_edits(edits_to_apply)

    def _generate_patch_from_file_blocks(self, plan: ImprovementPlan) -> str:
        if not plan.target_files:
            raise RuntimeError("file-block mode requires target_files from planner")

        file_context = self.repo.read_target_files(plan.target_files, self.config.target_file_context_bytes)
        target_list = [self._normalize_rel_path(path) for path in plan.target_files]

        system_prompt = "You rewrite files and return only file blocks."
        user_prompt = (
            f"Objective: {plan.objective}\n"
            f"Rationale: {plan.rationale}\n"
            "Return one or more blocks in this exact format:\n"
            "<<<FILE:relative/path.py>>>\n"
            "FULL FILE CONTENT\n"
            "<<<END FILE>>>\n\n"
            "Rules:\n"
            "- path must be one of target_files\n"
            "- content must be complete final file text\n"
            "- output only these blocks, no prose\n\n"
            f"target_files={json.dumps(target_list)}\n\n"
            f"Current files:\n{file_context}"
        )
        raw = self.llm.generate(
            prompt=user_prompt,
            system=system_prompt,
            options=OllamaOptions(
                temperature=self.policy.coder_temperature,
                num_predict=self.policy.num_predict,
            ),
        )

        pattern = re.compile(r"<<<FILE:(?P<path>[^>]+)>>>\s*\n(?P<content>.*?)\n<<<END FILE>>>", re.DOTALL)
        matches = list(pattern.finditer(raw))
        if not matches:
            raise RuntimeError("no file blocks found in model response")

        edits: list[tuple[str, str]] = []
        for match in matches:
            path_raw = match.group("path")
            content_raw = match.group("content")
            rel_path = self._normalize_rel_path(path_raw)
            if rel_path not in target_list:
                continue
            edits.append((rel_path, content_raw.replace("\r\n", "\n")))

        if not edits:
            raise RuntimeError("file blocks did not include allowed target files")

        return self._build_patch_from_content_edits(edits)

    def _build_patch_from_content_edits(self, edits: list[tuple[str, str]]) -> str:
        patch_chunks: list[str] = []
        for rel_path, new_text in edits:
            file_path = (self.config.workspace / rel_path).resolve()
            if not file_path.exists() or not file_path.is_file():
                continue
            old_text = file_path.read_text(encoding="utf-8", errors="replace")
            if old_text == new_text:
                continue
            old_lines = old_text.splitlines()
            new_lines = new_text.splitlines()
            diff_lines = list(
                difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile=f"a/{rel_path}",
                    tofile=f"b/{rel_path}",
                    lineterm="",
                )
            )
            if not diff_lines:
                continue
            patch_chunks.append(f"diff --git a/{rel_path} b/{rel_path}")
            patch_chunks.extend(diff_lines)
            patch_chunks.append("")
        if not patch_chunks:
            raise RuntimeError("candidate edits did not produce any changes")
        return "\n".join(patch_chunks).rstrip() + "\n"

    @staticmethod
    def _normalize_rel_path(path_value: str) -> str:
        cleaned = path_value.replace("\\", "/").strip().lstrip("./")
        return str(PurePosixPath(cleaned))

    def _path_is_allowed(self, rel_path: str) -> bool:
        normalized_path = self._normalize_rel_path(rel_path)
        for allowed in self.config.allowed_paths:
            allowed_norm = self._normalize_rel_path(allowed)
            if normalized_path == allowed_norm or normalized_path.startswith(allowed_norm + "/"):
                return True
        return False

    def _sanitize_target_files(self, targets: list[str]) -> list[str]:
        sanitized: list[str] = []
        seen: set[str] = set()
        for raw in targets:
            if not isinstance(raw, str) or not raw.strip():
                continue
            normalized = self._normalize_rel_path(raw)
            if normalized.startswith("../") or normalized.startswith("/") or "/../" in normalized:
                continue
            if normalized in seen:
                continue
            if not self._path_is_allowed(normalized):
                continue

            full_path = (self.config.workspace / normalized).resolve()
            try:
                full_path.relative_to(self.config.workspace)
            except ValueError:
                continue

            # Allow editing existing files; for missing files require an existing parent.
            if full_path.exists():
                if not full_path.is_file():
                    continue
            else:
                parent = full_path.parent
                if not parent.exists() or not parent.is_dir():
                    continue

            seen.add(normalized)
            sanitized.append(normalized)
            if len(sanitized) >= self.config.max_patch_paths:
                break
        return sanitized

    def _default_target_files(self) -> list[str]:
        candidates = [
            "self_improver/supervisor.py",
            "self_improver/memory.py",
            "self_improver/ollama.py",
            "self_improver/todo.py",
            "tests/test_utils.py",
            "tests/test_todo.py",
        ]
        existing = [path for path in candidates if (self.config.workspace / path).is_file()]
        return self._sanitize_target_files(existing)[:4]

    @staticmethod
    def _extract_file_hints_from_text(text: str) -> list[str]:
        hints = re.findall(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9_]+)", text)
        normalized = [item.replace("\\", "/").lstrip("./") for item in hints]
        unique: list[str] = []
        for item in normalized:
            if item not in unique:
                unique.append(item)
        return unique[:8]

    def _next_todo_entry(self) -> TodoEntry | None:
        if not self.config.todo_enabled:
            return None
        return self.todo_queue.peek()

    @staticmethod
    def _todo_resolved(
        entry: TodoEntry,
        plan: ImprovementPlan,
        changed_paths: list[str],
        validation_report: ValidationReport,
    ) -> bool:
        if not validation_report.passed:
            return False
        if not changed_paths:
            return False
        if plan.todo_text.strip().lower() == entry.text.strip().lower():
            return True
        return entry.text.strip().lower() in plan.objective.strip().lower()

    def _paths_within_targets(self, changed_paths: list[str], target_files: list[str]) -> bool:
        if not target_files:
            return True
        normalized_targets = [self._normalize_rel_path(path) for path in target_files]
        for changed in changed_paths:
            changed_norm = self._normalize_rel_path(changed)
            allowed = False
            for target in normalized_targets:
                if changed_norm == target or changed_norm.startswith(target + "/"):
                    allowed = True
                    break
            if not allowed:
                return False
        return True

    @staticmethod
    def _is_hard_reject_reason(reason: str) -> bool:
        text = reason.lower()
        hard_signals = [
            "malformed",
            "invalid diff",
            "outside allowlist",
            "path traversal",
            "syntax error",
            "broken tests",
        ]
        return any(signal in text for signal in hard_signals)

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
                json_mode=True,
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
        todo_entry = self._next_todo_entry()
        return {
            "config": {
                "workspace": str(self.config.workspace),
                "model": self.config.model,
                "ollama_base_url": self.config.ollama_base_url,
                "todo_file": str(self.config.todo_path),
                "todo_enabled": self.config.todo_enabled,
            },
            "memory": self.memory.stats(),
            "policy": json.loads(self.policy.to_json()),
            "git_status": self.repo.status_short(),
            "todo": {
                "next": todo_entry.text if todo_entry is not None else "",
                "has_items": todo_entry is not None,
            },
        }
