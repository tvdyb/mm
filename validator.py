"""Post-task validation with multi-layer test loops.

Two testing loops:
1. INNER LOOP (inside Claude Code invocation):
   The system prompt tells Claude Code to run tests itself, read failures,
   and fix them before exiting. This happens within a single claude -p call.
   Claude Code is already good at this — we just need to prompt it properly.

2. OUTER LOOP (orchestrator level):
   After Claude Code exits, the orchestrator runs the validation command
   independently. If it fails, it re-invokes Claude Code with:
   - The specific test/build errors
   - The --continue flag to keep the same session context
   This repeats up to test_fix_cycles times before marking as failed.
"""

import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

# ─── Validation Results ─────────────────────────────────────────────────────


@dataclass
class ValidationResult:
    passed: bool
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def error_summary(self) -> str:
        """Extract the most useful error info for feeding back to Claude."""
        output = self.stderr.strip() or self.stdout.strip()
        if not output:
            return f"Command failed with exit code {self.exit_code}"
        return output

    @property
    def short_error(self) -> str:
        """Short version for Telegram notifications."""
        summary = self.error_summary
        lines = summary.split("\n")
        error_lines = [
            l for l in lines
            if any(kw in l.lower() for kw in ["error", "fail", "cannot", "not found", "unexpected"])
        ]
        if error_lines:
            return "\n".join(error_lines[:10])
        return "\n".join(lines[-10:])


@dataclass
class TestCycleResult:
    """Result of the full outer test-fix loop for a single task."""
    passed: bool
    cycles_used: int
    total_tokens: int = 0
    total_cost: float = 0.0
    validation_history: list[ValidationResult] = field(default_factory=list)
    fix_attempts: list[str] = field(default_factory=list)
    final_error: Optional[str] = None


# ─── Validation Execution ─────────────────────────────────────────────────


def run_validation(
    command: str,
    expect_exit_code: int = 0,
    cwd: Optional[str] = None,
    timeout: int = 180,
) -> ValidationResult:
    """Run a validation command and check the exit code."""
    start = time.time()
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = time.time() - start
        return ValidationResult(
            passed=result.returncode == expect_exit_code,
            command=command,
            exit_code=result.returncode,
            stdout=result.stdout[-5000:],
            stderr=result.stderr[-5000:],
            duration_seconds=duration,
        )
    except subprocess.TimeoutExpired:
        return ValidationResult(
            passed=False,
            command=command,
            exit_code=-1,
            stdout="",
            stderr=f"Validation timed out after {timeout}s",
            duration_seconds=timeout,
        )
    except Exception as e:
        return ValidationResult(
            passed=False,
            command=command,
            exit_code=-1,
            stdout="",
            stderr=str(e),
            duration_seconds=time.time() - start,
        )


def run_validation_suite(
    validations: list[dict],
    cwd: Optional[str] = None,
    repo_path: str = "",
) -> tuple[bool, list[ValidationResult]]:
    """
    Run multiple validation commands in sequence.
    Returns (all_passed, results). Stops at first failure.
    """
    results = []
    for v in validations:
        cmd = v["command"].replace("{repo_path}", repo_path)
        expect = v.get("expect_exit_code", 0)
        timeout = v.get("timeout", 180)

        result = run_validation(cmd, expect, cwd=cwd, timeout=timeout)
        results.append(result)
        if not result.passed:
            return False, results

    return True, results


# ─── Fix Prompt Builders ────────────────────────────────────────────────────


def build_fix_prompt(
    original_task_name: str,
    validation_result: ValidationResult,
    cycle_num: int,
    max_cycles: int,
) -> str:
    """Build a prompt that tells Claude Code to fix the test failures."""
    error_output = validation_result.error_summary

    return f"""The previous work on subtask '{original_task_name}' did not pass validation.

VALIDATION COMMAND: {validation_result.command}
EXIT CODE: {validation_result.exit_code}

ERROR OUTPUT:
{error_output[-3000:]}

This is fix attempt {cycle_num} of {max_cycles}. Please:
1. Read the error output carefully and identify the root cause
2. Fix the issue — do NOT start over, fix the existing code
3. After fixing, run the validation command yourself to verify: {validation_result.command}
4. If the validation still fails, read the NEW error and fix that too
5. Repeat until the validation passes or you've exhausted all approaches

Focus ONLY on fixing the validation errors. Do not refactor or add features."""


def build_inner_loop_system_prompt(
    task_name: str,
    repo_path: str,
    validations: list[dict],
) -> str:
    """
    Build the system prompt that tells Claude Code to self-test within a
    single invocation (the inner loop).
    """
    validation_cmds = []
    for v in validations:
        cmd = v["command"].replace("{repo_path}", repo_path)
        validation_cmds.append(cmd)

    cmds_str = "\n".join(f"  {cmd}" for cmd in validation_cmds)

    return f"""You are working on subtask '{task_name}' of a larger project.
The project repo is at {repo_path}.
Focus ONLY on this subtask. Do NOT modify files unrelated to this task.

CRITICAL — SELF-TESTING LOOP:
After you finish writing code, you MUST run the following validation commands:
{cmds_str}

If any validation fails:
1. Read the error output carefully
2. Identify and fix the root cause
3. Run the validation again
4. Repeat until ALL validations pass

Do NOT declare the task complete until all validations pass.
Do NOT skip validations or claim they would pass without running them.
This test-fix cycle is the most important part of your job.

Work incrementally: write code → test → fix → test → confirm passing."""


# ─── Git Utilities ──────────────────────────────────────────────────────────


def git_checkpoint(repo_path: str, message: str) -> bool:
    """Create a git checkpoint commit."""
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=repo_path,
            capture_output=True,
            check=True,
        )
        result = subprocess.run(
            ["git", "commit", "-m", f"[autopilot] {message}", "--allow-empty"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except subprocess.CalledProcessError:
        return False


def git_rollback(repo_path: str, commits_back: int = 1) -> bool:
    """Roll back to a previous git checkpoint."""
    try:
        result = subprocess.run(
            ["git", "reset", "--hard", f"HEAD~{commits_back}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except subprocess.CalledProcessError:
        return False


def git_stash(repo_path: str) -> bool:
    """Stash current changes (safety net before rollback)."""
    try:
        result = subprocess.run(
            ["git", "stash", "push", "-m", "autopilot-pre-rollback"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except subprocess.CalledProcessError:
        return False
