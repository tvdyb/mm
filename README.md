# Claude Autopilot v2 🤖✈️

Telegram-controlled orchestration for running massive Claude Code projects autonomously on your Mac Mini. Kick off a 10-hour build from your phone, get progress updates, and course-correct mid-flight.

## What's New in v2

- **CLAUDE.md auto-generation** — maintains a living context file so subtask 20 knows what subtask 3 decided
- **Parallel execution** — independent tasks run simultaneously via git worktrees (3x default)
- **Adaptive replanning** — DeepSeek re-evaluates remaining tasks against what was actually built
- **Cost guardrails** — per-task, per-project, and hourly burn rate caps with auto-pause
- **Semantic review** — DeepSeek checks if what Claude built matches the spec (not just "does it compile")
- **Robust fix loops** — pipes full error context into fix prompts instead of fragile --resume flags

## Architecture

```
You (Phone)
    │
    ▼
Telegram Bot (bot.py)  ──────────  DeepSeek API (planning, review, replanning)
    │
    ▼
Orchestrator (orchestrator.py)
    │
    ├── CLAUDE.md Manager (context.py)     — maintains project memory
    ├── Budget Guard (budget.py)           — enforces cost limits
    ├── Semantic Reviewer (review.py)      — validates output vs intent
    ├── Parallel Runner (parallel.py)      — git worktree concurrency
    ├── Validator (validator.py)           — build/test/lint + fix loops
    └── Adaptive Planner (planner.py)      — mid-project replanning
    │
    ▼
Claude Code (headless via `claude -p`)
    │
    ▼
Your Project Repository (git-controlled, with auto-maintained CLAUDE.md)
```

## Quick Start

```bash
# 1. Clone to Mac Mini
git clone <this-repo> ~/claude-autopilot && cd ~/claude-autopilot

# 2. Install
pip install -r requirements.txt

# 3. Configure
cp .env.example .env   # add your API keys

# 4. Launch in tmux
./launch.sh

# 5. From your phone:
#    /plan Build a prediction market dashboard with Kalshi/Polymarket data
#    /start
#    (Go to class. Check /status between lectures.)
```

## Dual-Loop Testing

Every subtask goes through two testing loops before being marked complete:

```
┌──────────────────────────────────────────┐
│  INNER LOOP (inside Claude Code)         │
│  System prompt instructs Claude to:      │
│  write → test → read errors → fix → test │
└──────────────────────────────────────────┘
                    │
               Claude exits
                    │
                    ▼
┌──────────────────────────────────────────┐
│  OUTER LOOP (orchestrator)               │
│  Independently runs validation commands  │
│  If fail → re-invoke Claude with errors  │
│  Pipes previous output as context        │
│  Repeats up to test_fix_cycles times     │
└──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────┐
│  SEMANTIC REVIEW (DeepSeek)              │
│  Compares git diff against task spec     │
│  Catches "compiles but wrong" issues     │
│  Can block completion if verdict = fail  │
└──────────────────────────────────────────┘
                    │
                    ▼
         Update CLAUDE.md → git commit → next task
```

## Parallel Execution

When multiple tasks have their dependencies met simultaneously:

```yaml
tasks:
  - id: "db-setup"    # ← completes
  - id: "auth"        # ← depends on db-setup  ┐
  - id: "billing"     # ← depends on db-setup  ├─ run in parallel
  - id: "settings"    # ← depends on db-setup  ┘
```

Each parallel task gets its own git worktree (isolated copy of the repo). Results merge back via git. If there's a merge conflict, the task retries sequentially. Set `max_parallel: 3` (default) to control concurrency and API costs.

## Adaptive Replanning

After every N completed tasks (default: 8), DeepSeek reads the CLAUDE.md and reviews remaining task prompts against what was actually built. It adjusts prompts for tasks that reference incorrect file names, assume different architecture, or duplicate completed work.

## Cost Guardrails

Three budget caps that auto-pause and alert you:

| Guard | Default | Triggers when |
|-------|---------|---------------|
| Per-task | $5.00 | Single subtask exceeds limit |
| Per-project | $75.00 | Total project spend exceeds limit |
| Hourly burn | $15.00/hr | Sustained burn rate too high |

All configurable in the YAML settings block or .env.

## Task YAML Schema

```yaml
project:
  name: "my-project"
  repo_path: "/Users/wilson/projects/my-project"
  description: "One-line description"

settings:
  max_retries: 2              # full re-attempts before pause
  max_turns_per_task: 25      # Claude Code turns for initial execution
  checkpoint_every: 3         # git commit every N tasks
  notify_every: 5             # Telegram update every N tasks
  validation: "auto"          # auto | manual | skip
  test_fix_cycles: 3          # outer loop fix attempts
  fix_max_turns: 15           # turns per fix cycle
  max_parallel: 3             # concurrent task limit
  replan_every: 8             # adaptive replan interval
  semantic_review: true       # DeepSeek output review
  task_budget: 5.0            # $/task cap
  project_budget: 75.0        # $/project cap
  hourly_budget: 15.0         # $/hr cap

tasks:
  - id: "unique-id"
    name: "Human-readable name"
    prompt: |
      Detailed instructions for Claude Code...
    depends_on: ["other-task-id"]
    validations:
      - command: "cd {repo_path} && npx tsc --noEmit"
        expect_exit_code: 0
      - command: "cd {repo_path} && npm test -- --watchAll=false"
        expect_exit_code: 0
    max_turns: 25
    priority: "critical"
```

## Commands

| Command | Description |
|---------|-------------|
| `/plan <desc>` | DeepSeek decomposes project into YAML |
| `/plan <desc> \| <path>` | With custom repo path |
| `/start` | Begin from first incomplete task |
| `/start <file.yaml>` | Load and start specific plan |
| `/pause` | Pause after current task |
| `/resume` | Resume execution |
| `/abort` | Kill immediately |
| `/status` | Progress, current task, budget |
| `/tasks` | All tasks with status icons |
| `/log` | Last 50 lines of output |
| `/log full` | Full output as file |
| `/cost` | Budget tracking + burn rate |
| `/retry` | Retry last failed task |
| `/skip` | Skip failed, continue |
| `/fix` | DeepSeek fix suggestion |
| `/edit <id> <prompt>` | Edit task prompt |
| `/inject <msg>` | Add instructions to next task |

## Safety

Runs Claude Code with `--dangerously-skip-permissions` mitigated by:

- **Git checkpoints** every N tasks — `git reset --hard` to any checkpoint
- **Scoped working directory** — never runs in `~`, always in project repo
- **Task isolation** — system prompt restricts Claude to task-relevant files
- **Validation gates** — build/test must pass before proceeding
- **Semantic review** — DeepSeek catches "builds but wrong"
- **Budget caps** — auto-pause before runaway spending
- **Merge conflict handling** — parallel tasks fail gracefully to sequential
- **Crash recovery** — state.json persists across restarts
