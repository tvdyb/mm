"""Semantic review: checks if what Claude built matches the task intent.

Build/test validation catches "does it compile." Semantic review catches
"did it build the right thing." This is critical for long autonomous runs
where Claude can drift from the spec without triggering build errors.

Uses DeepSeek (cheap) to compare the git diff against the original task
prompt and flag potential mismatches before moving to the next subtask.
"""
import json
import subprocess
import logging
from typing import Optional

from openai import OpenAI

from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL

logger = logging.getLogger("autopilot.review")

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

REVIEW_PROMPT = """
You are a code reviewer checking if an AI coding agent's output matches the task specification.

TASK SPECIFICATION:
{task_prompt}

GIT DIFF (what was actually built):
{diff}

FILE LISTING (files changed):
{file_list}

Evaluate on these criteria:
1. COMPLETENESS: Were all requested features/files created? List any missing items.
2. CORRECTNESS: Does the implementation match what was asked? Flag any deviations.
3. QUALITY: Any obvious issues (hardcoded values that should be configurable, missing error handling, etc)?

Respond in this exact JSON format:
{{
  "verdict": "pass" | "warn" | "fail",
  "completeness_score": 0-10,
  "issues": ["list of specific issues found"],
  "missing": ["list of things requested but not implemented"],
  "summary": "one-sentence overall assessment"
}}

Rules:
- "pass" = everything looks good, minor issues at most
- "warn" = mostly correct but notable gaps, can proceed but worth noting
- "fail" = significant mismatch between spec and implementation
- Be specific about what's missing/wrong, not vague
- Don't fail for style preferences, only for functional gaps
- Output ONLY the JSON, no markdown fences
"""


def review_task_output(
    task_prompt: str,
    task_name: str,
    repo_path: str,
) -> dict:
    """
    Review the output of a completed task against its specification.

    Returns:
        dict with keys: verdict, completeness_score, issues, missing, summary
    """
    # Get the git diff
    diff = _get_task_diff(repo_path)
    file_list = _get_changed_files(repo_path)

    if not diff:
        return {
            "verdict": "warn",
            "completeness_score": 0,
            "issues": ["No git diff found — task may not have made any changes"],
            "missing": [],
            "summary": "No changes detected",
        }

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "user",
                    "content": REVIEW_PROMPT.format(
                        task_prompt=task_prompt[:2000],
                        diff=diff[:6000],
                        file_list=file_list[:1000],
                    ),
                },
            ],
            max_tokens=500,
            temperature=0.1,
        )

        text = response.choices[0].message.content.strip()

        # Strip markdown fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

        result = json.loads(text)

        # Validate structure
        result.setdefault("verdict", "warn")
        result.setdefault("completeness_score", 5)
        result.setdefault("issues", [])
        result.setdefault("missing", [])
        result.setdefault("summary", "Review completed")

        logger.info(
            f"Semantic review for '{task_name}': "
            f"{result['verdict']} (completeness: {result['completeness_score']}/10)"
        )
        return result

    except Exception as e:
        logger.error(f"Semantic review failed: {e}")
        return {
            "verdict": "warn",
            "completeness_score": 5,
            "issues": [f"Review failed: {str(e)}"],
            "missing": [],
            "summary": "Could not complete semantic review",
        }


def format_review_for_telegram(review: dict, task_name: str) -> str:
    """Format a review result for Telegram notification."""
    icons = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
    icon = icons.get(review["verdict"], "❓")

    msg = (
        f"{icon} <b>Review: {task_name}</b>\n"
        f"Score: {review['completeness_score']}/10\n"
        f"{review['summary']}"
    )

    if review["issues"]:
        issues = "\n".join(f"  • {i}" for i in review["issues"][:5])
        msg += f"\n\nIssues:\n{issues}"

    if review["missing"]:
        missing = "\n".join(f"  • {m}" for m in review["missing"][:5])
        msg += f"\n\nMissing:\n{missing}"

    return msg


# ─── Helpers ────────────────────────────────────────────────────────────────


def _get_task_diff(repo_path: str, max_chars: int = 8000) -> Optional[str]:
    """Get the diff from the most recent autopilot commit."""
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD~1..HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            diff = result.stdout
            return diff[:max_chars] + ("...(truncated)" if len(diff) > max_chars else "")
    except Exception:
        pass

    # Fallback: diff against working tree
    try:
        result = subprocess.run(
            ["git", "diff", "--staged"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout[:max_chars]
    except Exception:
        pass

    return None


def _get_changed_files(repo_path: str) -> str:
    """Get list of files changed in the most recent commit."""
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD~1..HEAD", "--name-status"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return "(could not determine changed files)"
