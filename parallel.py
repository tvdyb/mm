"""Parallel task execution using git worktrees.

When multiple tasks have their dependencies met simultaneously (e.g., "auth"
and "billing" both depend only on "db-setup"), they can run in parallel in
isolated git worktrees. Each worktree gets its own copy of the repo at the
current HEAD, Claude Code runs independently in each, and results are merged
back via git.

This can cut a 10-hour sequential build to 5-6 hours.

Limitations:
- Tasks that modify the same files will conflict at merge time
- Max parallelism is configurable (default: 3 to manage API costs)
- Merge conflicts pause execution and alert the user
"""

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("autopilot.parallel")

# ─── Worktree Management ───────────────────────────────────────────────────


def create_worktree(repo_path: str, task_id: str) -> Optional[str]:
    """
    Create a git worktree for parallel task execution.
    Returns the worktree path, or None on failure.
    """
    worktree_dir = Path(repo_path).parent / ".autopilot-worktrees"
    worktree_dir.mkdir(exist_ok=True)
    worktree_path = worktree_dir / task_id

    if worktree_path.exists():
        # Clean up stale worktree
        remove_worktree(repo_path, task_id)

    try:
        # Create a new branch for this task
        branch_name = f"autopilot/{task_id}"
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=repo_path,
            capture_output=True,
        )

        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_path)],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"Failed to create worktree for {task_id}: {result.stderr}")
            return None

        logger.info(f"Created worktree at {worktree_path} (branch: {branch_name})")
        return str(worktree_path)

    except Exception as e:
        logger.error(f"Worktree creation error: {e}")
        return None


def remove_worktree(repo_path: str, task_id: str):
    """Remove a git worktree after task completion."""
    worktree_path = Path(repo_path).parent / ".autopilot-worktrees" / task_id
    try:
        subprocess.run(
            ["git", "worktree", "remove", str(worktree_path), "--force"],
            cwd=repo_path,
            capture_output=True,
        )
        # Also delete the branch
        branch_name = f"autopilot/{task_id}"
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=repo_path,
            capture_output=True,
        )
        logger.info(f"Removed worktree for {task_id}")
    except Exception as e:
        logger.error(f"Failed to remove worktree for {task_id}: {e}")


def merge_worktree(repo_path: str, task_id: str) -> tuple[bool, str]:
    """
    Merge a completed task's worktree branch back into main.
    Returns (success, message).
    """
    branch_name = f"autopilot/{task_id}"
    try:
        # Switch to main branch
        result = subprocess.run(
            ["git", "merge", branch_name, "--no-edit", "-m",
             f"[autopilot] merge parallel task: {task_id}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            logger.info(f"Merged {branch_name} into main")
            remove_worktree(repo_path, task_id)
            return True, "Merged successfully"
        else:
            # Merge conflict
            # Abort the merge so we don't leave a dirty state
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=repo_path,
                capture_output=True,
            )
            conflict_msg = result.stdout + result.stderr
            logger.warning(f"Merge conflict for {task_id}: {conflict_msg[:500]}")
            return False, f"Merge conflict:\n{conflict_msg[:1000]}"

    except Exception as e:
        logger.error(f"Merge error for {task_id}: {e}")
        return False, str(e)


def cleanup_all_worktrees(repo_path: str):
    """Remove all autopilot worktrees."""
    worktree_dir = Path(repo_path).parent / ".autopilot-worktrees"
    if worktree_dir.exists():
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=repo_path,
                capture_output=True,
            )
            shutil.rmtree(worktree_dir, ignore_errors=True)
            logger.info("Cleaned up all worktrees")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


# ─── Dependency Analysis ───────────────────────────────────────────────────


def find_parallel_groups(
    task_order: list[str],
    task_defs: dict[str, dict],
    task_statuses: dict[str, str],
) -> list[list[str]]:
    """
    Identify groups of tasks that can run in parallel.

    Returns a list of groups, where each group is a list of task IDs
    whose dependencies are all completed.

    Example: if tasks B and C both depend only on A, and A is completed,
    then [B, C] is a parallel group.
    """
    # Find all tasks with met dependencies
    runnable = []
    for tid in task_order:
        if task_statuses.get(tid) != "pending":
            continue
        deps = task_defs.get(tid, {}).get("depends_on", [])
        if all(task_statuses.get(d) == "completed" for d in deps):
            runnable.append(tid)

    if len(runnable) <= 1:
        return [runnable] if runnable else []

    # Group by shared dependency set (tasks with same deps can safely run together)
    groups_by_deps: dict[tuple, list[str]] = {}
    for tid in runnable:
        deps = tuple(sorted(task_defs.get(tid, {}).get("depends_on", [])))
        groups_by_deps.setdefault(deps, []).append(tid)

    # Return all runnable tasks as one parallel group
    # (the orchestrator will cap concurrency via max_parallel)
    return [runnable]


# ─── Parallel Runner ───────────────────────────────────────────────────────


async def run_parallel_tasks(
    task_ids: list[str],
    execute_fn,  # async callable(task_id, worktree_path) -> result
    repo_path: str,
    max_parallel: int = 3,
) -> dict[str, tuple[bool, str]]:
    """
    Run multiple tasks in parallel using worktrees.

    Args:
        task_ids: list of task IDs to run in parallel
        execute_fn: async function that takes (task_id, working_dir) and returns result
        repo_path: main repo path
        max_parallel: max concurrent tasks

    Returns:
        dict of task_id -> (success, message)
    """
    semaphore = asyncio.Semaphore(max_parallel)
    results = {}

    async def _run_one(task_id: str):
        async with semaphore:
            # Create worktree
            worktree_path = await asyncio.get_event_loop().run_in_executor(
                None, lambda: create_worktree(repo_path, task_id)
            )
            if not worktree_path:
                results[task_id] = (False, "Failed to create worktree")
                return

            try:
                # Execute task in worktree
                result = await execute_fn(task_id, worktree_path)
                results[task_id] = result
            except Exception as e:
                results[task_id] = (False, str(e))
            finally:
                # Merge or cleanup happens in the orchestrator
                pass

    # Run all tasks concurrently (bounded by semaphore)
    await asyncio.gather(*[_run_one(tid) for tid in task_ids])
    return results
