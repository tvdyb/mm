"""CLAUDE.md auto-generation and maintenance.

Claude Code automatically reads CLAUDE.md from the repo root. This module
maintains a living context document that prevents architectural drift across
subtasks by giving each Claude Code invocation knowledge of:
- What's been built so far
- Key architectural decisions
- File structure and conventions
- What's coming next in the plan

After each completed subtask, the orchestrator calls update_claude_md()
which uses DeepSeek (cheap) to read the git diff and append a summary.
"""
import re
import subprocess
import logging
from pathlib import Path
from typing import Optional

from openai import OpenAI

from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL

logger = logging.getLogger("autopilot.context")

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# ─── Templates ──────────────────────────────────────────────────────────────

INITIAL_TEMPLATE = """\
# Project: {project_name}

> Auto-maintained by Claude Autopilot. Claude Code reads this file automatically.

## Overview
{description}

## Tech Stack
(Will be populated as the project is built)

## Architecture Decisions
(Will be populated as decisions are made)

## File Structure
```
(Will be populated after first task)
```

## Conventions
- Follow existing patterns when adding new files
- Use consistent naming conventions established in early tasks
- Check imports and types before declaring a task complete

## Completed Tasks
(Updated after each subtask)

## Current Context
(Updated before each subtask)
"""

SUMMARIZE_PROMPT = """
You are maintaining a CLAUDE.md context file for an AI coding agent working on a project.

Given the git diff below from a completed subtask, generate a CONCISE update block.
Include ONLY:
1. What files were created/modified (just filenames, not full paths unless ambiguous)
2. Key architectural decisions or patterns established
3. Any important conventions (naming, structure, imports) other tasks should follow
4. Tech stack additions (new dependencies, frameworks)

Be extremely concise — this is a reference doc, not documentation.
Max 8 lines. No markdown headers. Start with "- " bullet points.

SUBTASK: {task_name}

GIT DIFF (truncated):
{diff}
"""

TREE_PROMPT = """
Given this file tree of a project, produce a CONCISE summary of the structure
suitable for a CLAUDE.md file. Group by purpose (pages, components, lib, api, etc).
Use a compact tree format. Max 20 lines.

{tree}
"""

# ─── Core Functions ─────────────────────────────────────────────────────────


def init_claude_md(repo_path: str, project_name: str, description: str):
    """Create the initial CLAUDE.md in the repo root."""
    md_path = Path(repo_path) / "CLAUDE.md"
    if md_path.exists():
        logger.info("CLAUDE.md already exists, skipping init")
        return

    content = INITIAL_TEMPLATE.format(
        project_name=project_name,
        description=description,
    )
    md_path.write_text(content)
    logger.info(f"Created CLAUDE.md at {md_path}")


def update_claude_md(
    repo_path: str,
    task_name: str,
    task_id: str,
    upcoming_tasks: list[str],
):
    """
    Update CLAUDE.md after a completed subtask.
    Uses DeepSeek to summarize the git diff and updates the file structure.
    """
    md_path = Path(repo_path) / "CLAUDE.md"
    if not md_path.exists():
        logger.warning("CLAUDE.md not found, skipping update")
        return

    content = md_path.read_text()

    # 1. Get the git diff for the last commit(s) from this task
    diff = _get_recent_diff(repo_path)
    if not diff:
        diff = "(no diff available — task may not have committed changes)"

    # 2. Use DeepSeek to summarize what was built
    try:
        summary = _summarize_diff(task_name, diff)
    except Exception as e:
        logger.error(f"Failed to summarize diff: {e}")
        summary = f"- Completed: {task_name}"

    # 3. Append to "Completed Tasks" section
    completed_marker = "## Completed Tasks"
    if completed_marker in content:
        content = content.replace(
            completed_marker,
            f"{completed_marker}\n\n### {task_id}: {task_name}\n{summary}",
            1,
        )

    # 4. Update "Current Context" with what's coming next
    context_marker = "## Current Context"
    if context_marker in content:
        next_tasks_str = "\n".join(f"- {t}" for t in upcoming_tasks[:5])
        if not next_tasks_str:
            next_tasks_str = "- All tasks complete!"
        # Replace everything after the marker until end of file or next ##
        pattern = r"(## Current Context\n).*?(?=\n## |\Z)"
        replacement = f"## Current Context\nUpcoming tasks:\n{next_tasks_str}\n"
        content = re.sub(pattern, replacement, content, flags=re.DOTALL)

    # 5. Update file structure periodically
    tree = _get_file_tree(repo_path)
    if tree:
        try:
            tree_summary = _summarize_tree(tree)
            structure_marker = "## File Structure"
            if structure_marker in content:
                pattern = r"(## File Structure\n)```.*?```"
                replacement = f"## File Structure\n```\n{tree_summary}\n```"
                content = re.sub(pattern, replacement, content, flags=re.DOTALL)
        except Exception as e:
            logger.error(f"Failed to update file tree: {e}")

    md_path.write_text(content)
    logger.info(f"Updated CLAUDE.md after task '{task_id}'")


# ─── Helpers ────────────────────────────────────────────────────────────────


def _get_recent_diff(repo_path: str, max_chars: int = 6000) -> Optional[str]:
    """Get the git diff from the most recent commit(s)."""
    try:
        # Get diff of last commit
        result = subprocess.run(
            ["git", "diff", "HEAD~1..HEAD", "--stat", "--patch"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            diff = result.stdout
            if len(diff) > max_chars:
                # Keep stat summary + truncated patch
                lines = diff.split("\n")
                stat_end = next(
                    (i for i, l in enumerate(lines) if l.startswith("diff --git")),
                    len(lines),
                )
                stat = "\n".join(lines[:stat_end])
                remaining = max_chars - len(stat)
                patch = "\n".join(lines[stat_end:])[:remaining]
                diff = stat + "\n" + patch + "\n... (truncated)"
            return diff
    except Exception as e:
        logger.error(f"Failed to get git diff: {e}")
    return None


def _get_file_tree(repo_path: str) -> Optional[str]:
    """Get a compact file tree of the project."""
    try:
        # Use find with depth limit, excluding common noise
        result = subprocess.run(
            [
                "find", ".", "-maxdepth", "4",
                "-not", "-path", "*/node_modules/*",
                "-not", "-path", "*/.git/*",
                "-not", "-path", "*/.next/*",
                "-not", "-path", "*/__pycache__/*",
                "-not", "-path", "*/dist/*",
                "-not", "-path", "*/.venv/*",
                "-not", "-name", "*.lock",
                "-not", "-name", "package-lock.json",
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout[:4000]
    except Exception:
        pass
    return None


def _summarize_diff(task_name: str, diff: str) -> str:
    """Use DeepSeek to summarize a git diff."""
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role": "user",
                "content": SUMMARIZE_PROMPT.format(
                    task_name=task_name,
                    diff=diff[:5000],
                ),
            },
        ],
        max_tokens=300,
        temperature=0.2,
    )
    return response.choices[0].message.content.strip()


def _summarize_tree(tree: str) -> str:
    """Use DeepSeek to produce a compact tree summary."""
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "user", "content": TREE_PROMPT.format(tree=tree[:3000])},
        ],
        max_tokens=400,
        temperature=0.1,
    )
    return response.choices[0].message.content.strip()
