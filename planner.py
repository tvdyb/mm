"""DeepSeek-powered task decomposition for project planning."""
import json
import yaml
from openai import OpenAI

from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, TASKS_DIR

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

PLANNER_SYSTEM_PROMPT = """
You are a senior software architect who decomposes large projects into
sequential, dependency-ordered subtasks for an AI coding agent (Claude Code).

RULES:
1. Each task must be a self-contained unit of work that can be built and validated independently.
2. Tasks should be ordered so dependencies are built first.
3. Each task needs a clear, specific prompt that tells the coding agent exactly what to build.
4. CRITICAL: Include validation commands for EVERY task. The system runs a test-fix loop
   that re-invokes Claude with error output if validation fails, so good validations
   are essential. Use build checks, type checks, linting, and unit tests.
5. Keep individual tasks scoped to ~15-30 minutes of AI coding time.
6. A 10-hour project should have roughly 20-40 subtasks.
7. Include setup/init tasks at the start and integration/polish tasks at the end.
8. Each prompt should reference the project path as {repo_path}.
9. Be very specific about file paths, function names, and expected behavior.
10. For large features, break into: scaffold → core logic → tests → integration.
11. When a task creates new functionality, include a task afterward that writes tests for it.
12. Use validations (plural) for multiple validation commands per task when appropriate.
13. Each prompt should end with: "Write tests for the code you create if a test framework is set up."

OUTPUT FORMAT: Valid YAML matching this schema exactly:

```yaml
project:
  name: "<kebab-case-name>"
  repo_path: "{repo_path}"
  description: "<one-line description>"

settings:
  max_retries: 2
  max_turns_per_task: 25
  checkpoint_every: 3
  notify_every: 5
  validation: "auto"
  test_fix_cycles: 3
  fix_max_turns: 15

tasks:
  - id: "<unique-kebab-id>"
    name: "<human-readable name>"
    prompt: |
      <detailed prompt for Claude Code>
    depends_on: []  # list of task ids this depends on
    validations:           # use plural for multiple checks
      - command: "<shell command to validate, e.g. type check>"
        expect_exit_code: 0
      - command: "<second validation, e.g. tests>"
        expect_exit_code: 0
    max_turns: 25
    priority: "critical"  # critical | high | normal | low
```

Tips for good validations:
- Type checking: "cd {repo_path} && npx tsc --noEmit" for TypeScript
- Build: "cd {repo_path} && npm run build" for Next.js/React
- Tests: "cd {repo_path} && npm test -- --watchAll=false" for Jest
- Lint: "cd {repo_path} && npx next lint" or "npx eslint src/"
- File existence: "test -f {repo_path}/src/important-file.ts"
- Python: "cd {repo_path} && python -m pytest" or "python -m mypy src/"

Output ONLY the YAML, no markdown fences, no explanation.
"""


def plan_project(description: str, repo_path: str = "/Users/wilson/projects/new-project") -> tuple[str, str, int]:
    """
    Use DeepSeek to decompose a project description into a task YAML.
    Returns (yaml_content, yaml_path, task_count).
    """
    response = client.chat.completions.create(
        model="deepseek-reasoner",
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Decompose this project into subtasks:\n\n"
                    f"{description}\n\n"
                    f"Project repo path: {repo_path}"
                ),
            },
        ],
        max_tokens=8000,
        temperature=0.3,
    )

    yaml_content = response.choices[0].message.content.strip()

    # Strip markdown fences if present
    if yaml_content.startswith("```"):
        lines = yaml_content.split("\n")
        yaml_content = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    # Validate it's parseable YAML
    parsed = yaml.safe_load(yaml_content)
    project_name = parsed.get("project", {}).get("name", "unnamed-project")

    yaml_path = TASKS_DIR / f"{project_name}.yaml"
    yaml_path.write_text(yaml_content)

    task_count = len(parsed.get("tasks", []))
    return yaml_content, str(yaml_path), task_count


def summarize_progress(state_summary: str) -> str:
    """Use DeepSeek to generate a concise progress summary."""
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role": "system",
                "content": "Summarize this project state in 2-3 sentences. Be concise and actionable.",
            },
            {"role": "user", "content": state_summary},
        ],
        max_tokens=200,
        temperature=0.2,
    )
    return response.choices[0].message.content.strip()


def suggest_fix(task_name: str, error_output: str) -> str:
    """Use DeepSeek to suggest a fix for a failed task."""
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role": "system",
                "content": (
                    "A coding task failed. Suggest a concise fix or modified prompt. "
                    "Keep it under 100 words."
                ),
            },
            {
                "role": "user",
                "content": f"Task: {task_name}\n\nError:\n{error_output[-2000:]}",
            },
        ],
        max_tokens=300,
        temperature=0.3,
    )
    return response.choices[0].message.content.strip()


REPLAN_PROMPT = """
You are reviewing a partially-completed AI coding project. The coding agent has
completed some tasks and has remaining tasks queued. Based on what's actually been
built (described in CLAUDE.md), some remaining tasks may need their prompts adjusted.

COMPLETED TASKS:
{completed}

CURRENT PROJECT STATE (from CLAUDE.md):
{claude_md}

REMAINING TASKS:
{remaining}

For each remaining task that needs adjustment, output a JSON object:
{{
  "<task_id>": "<adjusted prompt text>"
}}

Only include tasks that NEED changes. Common reasons to adjust:
- The task references files/functions that were named differently than expected
- The task assumes a structure that differs from what was actually built
- The task duplicates something already done
- The task's approach conflicts with architectural decisions made earlier

If no tasks need adjustment, output: {{}}

Output ONLY the JSON, no explanation, no markdown fences.
"""


def replan_remaining(
    completed_names: list[str],
    remaining_tasks: list[tuple[str, dict]],
    claude_md: str,
) -> dict[str, str]:
    """
    Review remaining tasks against actual project state and adjust prompts.
    Returns dict of task_id -> new_prompt for tasks that need changes.
    """
    completed_str = "\n".join(f"- {name}" for name in completed_names)
    remaining_str = "\n".join(
        f"- [{tid}] {td.get('name', tid)}: {td.get('prompt', '')[:200]}"
        for tid, td in remaining_tasks[:15]  # limit to avoid token overflow
    )

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role": "user",
                "content": REPLAN_PROMPT.format(
                    completed=completed_str,
                    claude_md=claude_md[:3000],
                    remaining=remaining_str,
                ),
            },
        ],
        max_tokens=2000,
        temperature=0.2,
    )

    text = response.choices[0].message.content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    try:
        adjustments = json.loads(text)
        if isinstance(adjustments, dict):
            return adjustments
    except (json.JSONDecodeError, TypeError):
        pass

    return {}
