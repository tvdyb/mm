"""Core orchestrator v2: all six improvements integrated.

1. CLAUDE.md auto-generation — maintains project context across subtasks
2. Parallel execution — runs independent tasks concurrently via git worktrees
3. Adaptive replanning — DeepSeek re-evaluates plan after every N tasks
4. Cost guardrails — per-task/project/hourly budget caps
5. Semantic review — DeepSeek checks if output matches intent
6. Robust fix loops — pipes context directly instead of relying on --resume
"""

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

import yaml

from budget import BudgetGuard
from config import (
    ANTHROPIC_API_KEY,
    DEFAULT_CHECKPOINT_EVERY,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TURNS,
    DEFAULT_NOTIFY_EVERY,
    LOGS_DIR,
)
from context import init_claude_md, update_claude_md
from notifier import (
    notify_completed,
    notify_paused,
    notify_progress,
    notify_task_completed,
    notify_task_failed,
    notify_task_started,
    send_message,
)
from parallel import (
    cleanup_all_worktrees,
    create_worktree,
    find_parallel_groups,
    merge_worktree,
    remove_worktree,
)
from review import format_review_for_telegram, review_task_output
from state import ProjectState, TaskState
from validator import (
    TestCycleResult,
    ValidationResult,
    build_fix_prompt,
    build_inner_loop_system_prompt,
    git_checkpoint,
    run_validation,
)

logger = logging.getLogger("autopilot.orchestrator")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# Defaults
DEFAULT_TEST_FIX_CYCLES = 3
DEFAULT_FIX_MAX_TURNS = 15
DEFAULT_TASK_TIMEOUT = 2400
DEFAULT_FIX_TIMEOUT = 900
DEFAULT_MAX_PARALLEL = 3
DEFAULT_REPLAN_EVERY = 8


class Orchestrator:
    def __init__(self):
        self.state: Optional[ProjectState] = None
        self.paused = False
        self.abort = False
        self._current_process: Optional[subprocess.Popen] = None
        self._task_prompts: dict[str, dict] = {}
        self._settings: dict = {}
        self.budget = BudgetGuard()

    # ─── Project Loading ────────────────────────────────────────────────

    def load_project(self, yaml_path: str) -> ProjectState:
        with open(yaml_path) as f:
            data = yaml.safe_load(f)

        project = data.get("project", {})
        settings = data.get("settings", {})
        tasks = data.get("tasks", [])

        self.state = ProjectState(
            project_name=project.get("name", "unnamed"),
            task_yaml_path=yaml_path,
            repo_path=project.get("repo_path", ""),
        )

        self._settings = {
            "max_retries": settings.get("max_retries", DEFAULT_MAX_RETRIES),
            "max_turns_per_task": settings.get("max_turns_per_task", DEFAULT_MAX_TURNS),
            "checkpoint_every": settings.get("checkpoint_every", DEFAULT_CHECKPOINT_EVERY),
            "notify_every": settings.get("notify_every", DEFAULT_NOTIFY_EVERY),
            "validation": settings.get("validation", "auto"),
            "test_fix_cycles": settings.get("test_fix_cycles", DEFAULT_TEST_FIX_CYCLES),
            "fix_max_turns": settings.get("fix_max_turns", DEFAULT_FIX_MAX_TURNS),
            # New v2 settings
            "max_parallel": settings.get("max_parallel", DEFAULT_MAX_PARALLEL),
            "replan_every": settings.get("replan_every", DEFAULT_REPLAN_EVERY),
            "semantic_review": settings.get("semantic_review", True),
            "task_budget": settings.get("task_budget", 5.0),
            "project_budget": settings.get("project_budget", 75.0),
            "hourly_budget": settings.get("hourly_budget", 15.0),
        }

        # Configure budget guard
        self.budget.configure(self._settings)

        for task in tasks:
            tid = task["id"]
            self.state.tasks[tid] = TaskState(id=tid, name=task["name"])
            self.state.task_order.append(tid)
            self._task_prompts[tid] = task

        self.state.save()
        logger.info(f"Loaded project '{self.state.project_name}' with {len(tasks)} tasks")
        return self.state

    def resume_project(self) -> Optional[ProjectState]:
        self.state = ProjectState.load()
        if self.state is None:
            logger.error("No saved state found")
            return None

        if self.state.task_yaml_path and Path(self.state.task_yaml_path).exists():
            with open(self.state.task_yaml_path) as f:
                data = yaml.safe_load(f)
            for task in data.get("tasks", []):
                self._task_prompts[task["id"]] = task
            settings = data.get("settings", {})
            self._settings = settings
            # Ensure all v2 defaults
            for key, default in [
                ("test_fix_cycles", DEFAULT_TEST_FIX_CYCLES),
                ("fix_max_turns", DEFAULT_FIX_MAX_TURNS),
                ("max_parallel", DEFAULT_MAX_PARALLEL),
                ("replan_every", DEFAULT_REPLAN_EVERY),
                ("semantic_review", True),
                ("task_budget", 5.0),
                ("project_budget", 75.0),
                ("hourly_budget", 15.0),
            ]:
                self._settings.setdefault(key, default)

        self.budget.configure(self._settings)
        self.budget.start_project(
            self.state.started_at or time.time(),
            self.state.total_cost,
        )

        for ts in self.state.tasks.values():
            if ts.status == "running":
                ts.status = "pending"
                logger.info(f"Reset crashed task '{ts.id}' to pending")

        self.state.save()
        logger.info(f"Resumed project '{self.state.project_name}'")
        return self.state

    # ─── Main Execution Loop ────────────────────────────────────────────

    async def run(self):
        if not self.state:
            logger.error("No project loaded")
            return

        self.state.started_at = self.state.started_at or time.time()
        self.state.status = "running"
        self.state.save()
        self.paused = False
        self.abort = False

        # [IMPROVEMENT 1] Initialize CLAUDE.md
        project_desc = ""
        if self.state.task_yaml_path and Path(self.state.task_yaml_path).exists():
            with open(self.state.task_yaml_path) as f:
                data = yaml.safe_load(f)
            project_desc = data.get("project", {}).get("description", "")
        self._ensure_repo_exists()
        init_claude_md(self.state.repo_path, self.state.project_name, project_desc)

        # [IMPROVEMENT 4] Initialize budget tracking
        self.budget.start_project(self.state.started_at, self.state.total_cost)

        completed_streak = 0
        tasks_since_replan = 0

        while True:
            if self.abort:
                self.state.status = "paused"
                self.state.save()
                await notify_paused("Aborted by user", self.state.progress_bar())
                return

            if self.paused:
                self.state.status = "paused"
                self.state.save()
                await notify_paused("Paused by user", self.state.progress_bar())
                return

            # [IMPROVEMENT 4] Check budget before starting next task
            budget_status = self.budget.check_budget(time.time())
            if not budget_status.within_budget:
                self.state.status = "paused"
                self.state.save()
                await send_message(budget_status.format_telegram())
                await notify_paused(
                    f"Budget exceeded: {budget_status.reason}",
                    self.state.progress_bar(),
                )
                return

            # [IMPROVEMENT 3] Adaptive replanning
            replan_every = self._settings.get("replan_every", DEFAULT_REPLAN_EVERY)
            if tasks_since_replan >= replan_every and replan_every > 0:
                await self._adaptive_replan()
                tasks_since_replan = 0

            # [IMPROVEMENT 2] Find parallel task groups
            max_parallel = self._settings.get("max_parallel", DEFAULT_MAX_PARALLEL)
            runnable = self._find_all_runnable()

            if not runnable:
                counts = self.state.count_by_status()
                if counts["failed"] > 0:
                    self.state.status = "failed"
                    self.state.save()
                    await notify_paused(
                        f"{counts['failed']} failed task(s) blocking progress",
                        self.state.progress_bar(),
                    )
                else:
                    self.state.status = "completed"
                    self.state.save()
                    total_time = time.time() - (self.state.started_at or time.time())
                    await notify_completed(
                        self.state.project_name,
                        len(self.state.tasks),
                        self.budget.total_cost,
                        total_time,
                    )
                return

            # Check retry limits for all runnable tasks
            max_retries = self._settings.get("max_retries", DEFAULT_MAX_RETRIES)
            executable = [
                (ts, td) for ts, td in runnable
                if ts.attempts < max_retries
            ]

            if not executable:
                # All runnable tasks have exhausted retries
                first_failed = runnable[0][0]
                self.state.status = "paused"
                self.state.save()
                await notify_paused(
                    f"Task '{first_failed.name}' failed {max_retries} times.\n"
                    f"Use /retry, /skip, or /edit to continue.",
                    self.state.progress_bar(),
                )
                return

            # Decide: parallel or sequential
            if len(executable) > 1 and max_parallel > 1:
                # Run multiple tasks in parallel
                batch = executable[:max_parallel]
                batch_results = await self._run_parallel_batch(batch)
                for task_state, task_def, success in batch_results:
                    if success:
                        completed_streak += 1
                        tasks_since_replan += 1
                    else:
                        completed_streak = 0
            else:
                # Sequential execution (single task)
                next_task, task_def = executable[0]
                success = await self._run_single_task(next_task, task_def)
                if success:
                    completed_streak += 1
                    tasks_since_replan += 1
                else:
                    completed_streak = 0

            # Git checkpoint
            checkpoint_every = self._settings.get("checkpoint_every", DEFAULT_CHECKPOINT_EVERY)
            if completed_streak > 0 and completed_streak % checkpoint_every == 0:
                git_checkpoint(self.state.repo_path, f"checkpoint #{completed_streak}")
                logger.info("Git checkpoint created")

            # Periodic progress notification
            notify_every = self._settings.get("notify_every", DEFAULT_NOTIFY_EVERY)
            if completed_streak > 0 and completed_streak % notify_every == 0:
                counts = self.state.count_by_status()
                counts["cost"] = self.budget.total_cost
                await notify_progress(
                    self.state.progress_bar(),
                    "batch" if len(executable) > 1 else executable[0][0].name,
                    counts,
                )

    # ─── Single Task Execution ──────────────────────────────────────────

    async def _run_single_task(
        self, task_state: TaskState, task_def: dict
    ) -> bool:
        """Execute a single task with test loop, review, and CLAUDE.md update."""
        task_num = self.state.task_order.index(task_state.id) + 1
        total = len(self.state.tasks)
        max_retries = self._settings.get("max_retries", DEFAULT_MAX_RETRIES)

        await notify_task_started(task_state.name, task_num, total)
        self.state.mark_task_running(task_state.id)
        self.budget.start_task()

        # Execute with test-fix loop
        cycle_result = await self._execute_with_test_loop(
            task_state.id, task_def, self.state.repo_path
        )

        if cycle_result.passed:
            # [IMPROVEMENT 5] Semantic review
            if self._settings.get("semantic_review", True):
                review = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: review_task_output(
                        task_def.get("prompt", ""),
                        task_def.get("name", task_state.id),
                        self.state.repo_path,
                    ),
                )
                if review["verdict"] == "fail":
                    # Semantic review failed — treat as task failure
                    error_msg = (
                        f"Semantic review FAILED: {review['summary']}\n"
                        f"Missing: {', '.join(review.get('missing', []))}"
                    )
                    self.state.mark_task_failed(task_state.id, error_msg)
                    await send_message(format_review_for_telegram(review, task_state.name))
                    await notify_task_failed(
                        task_state.name, error_msg[:500],
                        task_state.attempts, max_retries,
                    )
                    return False
                elif review["verdict"] == "warn":
                    await send_message(format_review_for_telegram(review, task_state.name))

            # Task passed! Commit and update context
            duration = time.time() - (task_state.started_at or time.time())
            self.state.mark_task_completed(
                task_state.id,
                tokens=cycle_result.total_tokens,
                cost=cycle_result.total_cost,
            )
            git_checkpoint(self.state.repo_path, f"task: {task_state.name}")

            # [IMPROVEMENT 1] Update CLAUDE.md
            upcoming = [
                self.state.tasks[tid].name
                for tid in self.state.task_order
                if self.state.tasks[tid].status == "pending"
            ]
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: update_claude_md(
                    self.state.repo_path,
                    task_state.name,
                    task_state.id,
                    upcoming,
                ),
            )

            cycles_msg = ""
            if cycle_result.cycles_used > 0:
                cycles_msg = f" (🔁 {cycle_result.cycles_used} fix cycles)"
            await notify_task_completed(task_state.name, task_num, total, duration)
            if cycles_msg:
                await send_message(cycles_msg)

            return True
        else:
            self.state.mark_task_failed(
                task_state.id,
                (cycle_result.final_error or "Unknown error")[-1000:],
            )
            await notify_task_failed(
                task_state.name,
                (cycle_result.final_error or "")[-500:],
                task_state.attempts,
                max_retries,
            )
            if cycle_result.cycles_used > 0:
                await send_message(
                    f"🔁 Ran {cycle_result.cycles_used} fix cycle(s) but validation still fails.\n"
                    f"Use /fix for analysis, or /edit {task_state.id} to rewrite."
                )
            return False

    # ─── Parallel Batch Execution ───────────────────────────────────────

    async def _run_parallel_batch(
        self, batch: list[tuple[TaskState, dict]]
    ) -> list[tuple[TaskState, dict, bool]]:
        """Run multiple tasks in parallel via worktrees."""
        task_ids = [ts.id for ts, _ in batch]
        await send_message(
            f"⚡ <b>Running {len(batch)} tasks in parallel:</b>\n"
            + "\n".join(f"  • {td.get('name', ts.id)}" for ts, td in batch)
        )

        results = []

        async def _execute_in_worktree(task_state, task_def):
            """Execute a task in its own worktree."""
            worktree_path = await asyncio.get_event_loop().run_in_executor(
                None, lambda: create_worktree(self.state.repo_path, task_state.id)
            )
            if not worktree_path:
                return False

            self.state.mark_task_running(task_state.id)
            self.budget.start_task()

            cycle_result = await self._execute_with_test_loop(
                task_state.id, task_def, worktree_path
            )

            if cycle_result.passed:
                # Merge worktree back
                merge_ok, merge_msg = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: merge_worktree(self.state.repo_path, task_state.id),
                )
                if merge_ok:
                    self.state.mark_task_completed(
                        task_state.id,
                        tokens=cycle_result.total_tokens,
                        cost=cycle_result.total_cost,
                    )
                    return True
                else:
                    # Merge conflict — fall back, will retry sequentially
                    self.state.mark_task_failed(
                        task_state.id,
                        f"Merge conflict: {merge_msg[:500]}",
                    )
                    await send_message(
                        f"⚠️ Merge conflict for <code>{task_state.name}</code>. "
                        f"Will retry sequentially after other parallel tasks finish."
                    )
                    # Reset to pending so sequential execution picks it up
                    self.state.tasks[task_state.id].status = "pending"
                    self.state.tasks[task_state.id].attempts -= 1  # don't count against retries
                    self.state.save()
                    return False
            else:
                self.state.mark_task_failed(
                    task_state.id,
                    (cycle_result.final_error or "Unknown")[-1000:],
                )
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: remove_worktree(self.state.repo_path, task_state.id)
                )
                return False

        # Run all in parallel
        tasks = [_execute_in_worktree(ts, td) for ts, td in batch]
        task_results = await asyncio.gather(*tasks, return_exceptions=True)

        for (ts, td), result in zip(batch, task_results):
            if isinstance(result, Exception):
                results.append((ts, td, False))
            else:
                results.append((ts, td, result))

        return results

    # ─── Test-Fix Loop ──────────────────────────────────────────────────

    async def _execute_with_test_loop(
        self, task_id: str, task_def: dict, working_dir: str
    ) -> TestCycleResult:
        """Execute a task with inner + outer test loops."""
        test_fix_cycles = self._settings.get("test_fix_cycles", DEFAULT_TEST_FIX_CYCLES)
        result = TestCycleResult(passed=False, cycles_used=0)

        # Step 1: Initial execution with inner-loop prompting
        success, output, tokens, cost = await self._execute_claude(
            task_id, task_def, working_dir, is_fix=False
        )
        result.total_tokens += tokens
        result.total_cost += cost

        # [IMPROVEMENT 4] Track cost
        self.budget.record_cost(cost, tokens)
        budget_check = self.budget.check_budget(time.time())
        if not budget_check.within_budget:
            result.final_error = f"Budget exceeded: {budget_check.reason}"
            return result

        # Step 2: Independent validation
        validations = self._get_validations(task_def)
        if not validations or self._settings.get("validation") != "auto":
            result.passed = success
            if not success:
                result.final_error = output[-1000:] if output else "Claude Code failed"
            return result

        val_result = self._run_task_validation(task_def, working_dir)
        if val_result.passed:
            result.passed = True
            return result

        result.validation_history.append(val_result)

        # Step 3: Outer test-fix loop
        # [IMPROVEMENT 6] Pipe context directly instead of --resume
        last_output = output  # carry forward for context

        for cycle in range(1, test_fix_cycles + 1):
            if self.abort or self.paused:
                result.final_error = "Interrupted during test-fix loop"
                return result

            # Budget check each cycle
            budget_check = self.budget.check_budget(time.time())
            if not budget_check.within_budget:
                result.final_error = f"Budget exceeded during fix cycle: {budget_check.reason}"
                return result

            logger.info(f"Task '{task_id}' — fix cycle {cycle}/{test_fix_cycles}")
            await send_message(
                f"🔧 <b>Fix cycle {cycle}/{test_fix_cycles}</b> for "
                f"<code>{task_def.get('name', task_id)}</code>\n"
                f"Error: <pre>{val_result.short_error[:500]}</pre>"
            )

            # [IMPROVEMENT 6] Build fix prompt with piped context
            fix_prompt = build_fix_prompt(
                task_def.get("name", task_id),
                val_result,
                cycle,
                test_fix_cycles,
            )

            # Include previous output as context (more robust than --resume)
            if last_output:
                context_snippet = last_output[-2000:] if len(last_output) > 2000 else last_output
                fix_prompt = (
                    f"CONTEXT FROM PREVIOUS EXECUTION:\n"
                    f"```\n{context_snippet}\n```\n\n"
                    f"{fix_prompt}"
                )

            fix_success, fix_output, fix_tokens, fix_cost = await self._execute_claude(
                task_id, task_def, working_dir,
                is_fix=True, fix_prompt=fix_prompt,
            )
            result.total_tokens += fix_tokens
            result.total_cost += fix_cost
            result.cycles_used = cycle
            self.budget.record_cost(fix_cost, fix_tokens)
            last_output = fix_output

            # Re-validate
            val_result = self._run_task_validation(task_def, working_dir)
            result.validation_history.append(val_result)

            if val_result.passed:
                logger.info(f"Task '{task_id}' passed after {cycle} fix cycle(s)")
                result.passed = True
                return result

        result.final_error = (
            f"Failed after {test_fix_cycles} fix cycles. "
            f"Last error: {val_result.short_error[:500]}"
        )
        return result

    # ─── Claude Code Invocation ─────────────────────────────────────────

    async def _execute_claude(
        self,
        task_id: str,
        task_def: dict,
        working_dir: str,
        is_fix: bool = False,
        fix_prompt: Optional[str] = None,
    ) -> tuple[bool, str, int, float]:
        """
        Execute a single Claude Code invocation.
        [IMPROVEMENT 6] No --resume dependency — all context piped via prompt.
        Returns (success, output_text, tokens_used, cost_usd).
        """
        if is_fix:
            prompt = fix_prompt or ""
            max_turns = self._settings.get("fix_max_turns", DEFAULT_FIX_MAX_TURNS)
            timeout = DEFAULT_FIX_TIMEOUT
        else:
            prompt = task_def.get("prompt", "")
            max_turns = task_def.get(
                "max_turns",
                self._settings.get("max_turns_per_task", DEFAULT_MAX_TURNS),
            )
            timeout = DEFAULT_TASK_TIMEOUT

        # Inject queued instructions
        if not is_fix and self.state and self.state.injected_instructions:
            extra = "\n\nADDITIONAL INSTRUCTIONS:\n" + "\n".join(
                self.state.injected_instructions
            )
            prompt += extra
            self.state.injected_instructions.clear()
            self.state.save()

        # Build system prompt
        validations = self._get_validations(task_def)
        if validations and not is_fix:
            system_append = build_inner_loop_system_prompt(
                task_def.get("name", task_id),
                working_dir,
                validations,
            )
        elif is_fix:
            system_append = (
                f"You are fixing validation failures for subtask "
                f"'{task_def.get('name', task_id)}'. "
                f"The project repo is at {working_dir}. "
                f"Focus ONLY on fixing the errors. Do not refactor or add features. "
                f"After fixing, run the validation command to confirm."
            )
        else:
            system_append = (
                f"You are working on subtask '{task_def.get('name', task_id)}'. "
                f"The project repo is at {working_dir}. "
                f"Focus ONLY on this subtask. Do NOT modify unrelated files. "
                f"Work incrementally."
            )

        cmd = [
            "claude",
            "-p", prompt,
            "--dangerously-skip-permissions",
            "--output-format", "json",
            "--max-turns", str(max_turns),
            "--append-system-prompt", system_append,
        ]

        env = os.environ.copy()
        if ANTHROPIC_API_KEY:
            env["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY
        # If no API key, Claude Code falls back to OAuth session

        phase = "fix" if is_fix else "initial"
        log_path = LOGS_DIR / f"{task_id}_{phase}_{int(time.time())}.log"

        logger.info(
            f"{'Fixing' if is_fix else 'Executing'} task '{task_id}' "
            f"(max_turns={max_turns}, timeout={timeout}s, cwd={working_dir})"
        )

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, cwd=working_dir, capture_output=True,
                    text=True, env=env, timeout=timeout,
                ),
            )

            output = result.stdout
            stderr = result.stderr

            log_path.write_text(
                f"COMMAND: {' '.join(cmd[:6])}...\n"
                f"PHASE: {phase}\nCWD: {working_dir}\n"
                f"EXIT CODE: {result.returncode}\n\n"
                f"STDOUT:\n{output}\n\nSTDERR:\n{stderr}"
            )

            if self.state and task_id in self.state.tasks:
                self.state.tasks[task_id].output_log = str(log_path)

            tokens = 0
            cost = 0.0
            try:
                data = json.loads(output)
                tokens = data.get("usage", {}).get("total_tokens", 0) or 0
                cost = data.get("cost_usd", 0.0) or 0.0
                output_text = data.get("result", output)
            except (json.JSONDecodeError, TypeError):
                output_text = output

            return result.returncode == 0, output_text, tokens, cost

        except subprocess.TimeoutExpired:
            logger.error(f"Task '{task_id}' ({phase}) timed out")
            return False, f"Timed out after {timeout}s", 0, 0.0
        except FileNotFoundError:
            logger.error("Claude Code CLI not found")
            return False, "Claude Code CLI not found", 0, 0.0
        except Exception as e:
            logger.error(f"Task '{task_id}' ({phase}) error: {e}")
            return False, str(e), 0, 0.0

    # ─── Adaptive Replanning ────────────────────────────────────────────

    async def _adaptive_replan(self):
        """
        [IMPROVEMENT 3] Use DeepSeek to review remaining tasks against
        what's actually been built and adjust the plan.
        """
        try:
            from planner import replan_remaining

            completed_names = [
                self.state.tasks[tid].name
                for tid in self.state.task_order
                if self.state.tasks[tid].status == "completed"
            ]
            remaining = [
                (tid, self._task_prompts[tid])
                for tid in self.state.task_order
                if self.state.tasks[tid].status == "pending"
            ]

            if not remaining:
                return

            # Read CLAUDE.md for current project state
            claude_md = ""
            md_path = Path(self.state.repo_path) / "CLAUDE.md"
            if md_path.exists():
                claude_md = md_path.read_text()[:3000]

            await send_message("🧠 Adaptive replanning in progress...")

            adjustments = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: replan_remaining(
                    completed_names, remaining, claude_md,
                ),
            )

            if adjustments:
                for tid, new_prompt in adjustments.items():
                    if tid in self._task_prompts:
                        self._task_prompts[tid]["prompt"] = new_prompt
                        logger.info(f"Replanned task '{tid}'")

                await send_message(
                    f"🧠 Adjusted {len(adjustments)} upcoming task(s) based on current project state."
                )

        except ImportError:
            logger.warning("Replanning not available — planner module missing replan_remaining")
        except Exception as e:
            logger.error(f"Adaptive replanning failed: {e}")

    # ─── Validation Helpers ─────────────────────────────────────────────

    def _get_validations(self, task_def: dict) -> list[dict]:
        validations = task_def.get("validations", [])
        single = task_def.get("validation")
        if single and not validations:
            validations = [single]
        return validations

    def _run_task_validation(
        self, task_def: dict, working_dir: Optional[str] = None
    ) -> ValidationResult:
        cwd = working_dir or self.state.repo_path
        validations = self._get_validations(task_def)
        result = None
        for v in validations:
            cmd = v["command"].replace("{repo_path}", cwd)
            result = run_validation(
                command=cmd,
                expect_exit_code=v.get("expect_exit_code", 0),
                cwd=cwd,
                timeout=v.get("timeout", 180),
            )
            if not result.passed:
                return result
        return result

    # ─── Task Finding ───────────────────────────────────────────────────

    def _find_all_runnable(self) -> list[tuple[TaskState, dict]]:
        """Find ALL tasks with met dependencies (for parallel execution)."""
        runnable = []
        for tid in self.state.task_order:
            ts = self.state.tasks[tid]
            if ts.status != "pending":
                continue
            td = self._task_prompts.get(tid, {})
            deps = td.get("depends_on", [])
            if all(
                self.state.tasks.get(d, TaskState(id=d, name="")).status == "completed"
                for d in deps
            ):
                runnable.append((ts, td))
        return runnable

    def _find_next_runnable(self) -> tuple[Optional[TaskState], Optional[dict]]:
        runnable = self._find_all_runnable()
        return runnable[0] if runnable else (None, None)

    # ─── Utility ────────────────────────────────────────────────────────

    def _ensure_repo_exists(self):
        """Create repo directory and init git if needed."""
        repo = Path(self.state.repo_path)
        if not repo.exists():
            repo.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "Initial commit"],
                cwd=str(repo), capture_output=True,
            )
            logger.info(f"Created repo at {repo}")

    # ─── Control Signals ────────────────────────────────────────────────

    def pause(self):
        self.paused = True

    def abort_now(self):
        self.abort = True
        if self._current_process:
            self._current_process.kill()

    def inject(self, instructions: str):
        if self.state:
            self.state.injected_instructions.append(instructions)
            self.state.save()


# Singleton
orchestrator = Orchestrator()
