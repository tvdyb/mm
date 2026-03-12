"""Cost guardrails: budget caps that pause execution before you wake up to a $500 bill.

Tracks token usage and estimated cost per task and project-wide.
Pauses execution and sends an alert if either threshold is exceeded.

Cost estimation is based on Claude Code's JSON output which includes
cost_usd when using --output-format json. As a fallback, we estimate
from token counts using published Sonnet pricing.
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("autopilot.budget")

# Approximate pricing for Claude Sonnet (March 2026)
# These are estimates — actual pricing may differ
PRICING = {
    "sonnet": {"input_per_m": 3.0, "output_per_m": 15.0},
    "opus": {"input_per_m": 15.0, "output_per_m": 75.0},
    "haiku": {"input_per_m": 0.25, "output_per_m": 1.25},
}

# Defaults
DEFAULT_TASK_BUDGET = 5.0       # $5 per subtask
DEFAULT_PROJECT_BUDGET = 75.0   # $75 per project
DEFAULT_HOURLY_BUDGET = 15.0    # $15/hr burn rate cap


@dataclass
class BudgetStatus:
    within_budget: bool
    task_cost: float
    project_cost: float
    task_budget: float
    project_budget: float
    reason: Optional[str] = None  # why we're over budget

    @property
    def task_pct(self) -> float:
        return (self.task_cost / self.task_budget * 100) if self.task_budget > 0 else 0

    @property
    def project_pct(self) -> float:
        return (self.project_cost / self.project_budget * 100) if self.project_budget > 0 else 0

    def format_telegram(self) -> str:
        if self.within_budget:
            return (
                f"💰 Task: ${self.task_cost:.2f}/${self.task_budget:.2f} "
                f"({self.task_pct:.0f}%) | "
                f"Project: ${self.project_cost:.2f}/${self.project_budget:.2f} "
                f"({self.project_pct:.0f}%)"
            )
        else:
            return (
                f"🚨 <b>BUDGET EXCEEDED</b>\n"
                f"Reason: {self.reason}\n"
                f"Task: ${self.task_cost:.2f}/${self.task_budget:.2f}\n"
                f"Project: ${self.project_cost:.2f}/${self.project_budget:.2f}"
            )


class BudgetGuard:
    """Tracks costs and enforces budget limits."""

    def __init__(
        self,
        task_budget: float = DEFAULT_TASK_BUDGET,
        project_budget: float = DEFAULT_PROJECT_BUDGET,
        hourly_budget: float = DEFAULT_HOURLY_BUDGET,
    ):
        self.task_budget = task_budget
        self.project_budget = project_budget
        self.hourly_budget = hourly_budget

        # Running totals
        self._current_task_cost = 0.0
        self._project_cost = 0.0
        self._project_start_time: Optional[float] = None

    def configure(self, settings: dict):
        """Update budget settings from project YAML."""
        self.task_budget = settings.get("task_budget", DEFAULT_TASK_BUDGET)
        self.project_budget = settings.get("project_budget", DEFAULT_PROJECT_BUDGET)
        self.hourly_budget = settings.get("hourly_budget", DEFAULT_HOURLY_BUDGET)

    def start_project(self, start_time: float, current_cost: float = 0.0):
        """Initialize project tracking."""
        self._project_start_time = start_time
        self._project_cost = current_cost

    def start_task(self):
        """Reset per-task tracking."""
        self._current_task_cost = 0.0

    def record_cost(self, cost_usd: float, tokens: int = 0):
        """
        Record cost from a Claude Code invocation.
        Uses reported cost if available, falls back to token estimate.
        """
        if cost_usd > 0:
            actual_cost = cost_usd
        elif tokens > 0:
            # Rough estimate: assume ~60% input, 40% output for typical coding tasks
            pricing = PRICING.get("sonnet", PRICING["sonnet"])
            input_tokens = int(tokens * 0.6)
            output_tokens = int(tokens * 0.4)
            actual_cost = (
                (input_tokens / 1_000_000 * pricing["input_per_m"])
                + (output_tokens / 1_000_000 * pricing["output_per_m"])
            )
        else:
            actual_cost = 0.0

        self._current_task_cost += actual_cost
        self._project_cost += actual_cost

    def check_budget(self, current_time: Optional[float] = None) -> BudgetStatus:
        """Check if we're still within budget."""
        # Check per-task budget
        if self._current_task_cost > self.task_budget:
            return BudgetStatus(
                within_budget=False,
                task_cost=self._current_task_cost,
                project_cost=self._project_cost,
                task_budget=self.task_budget,
                project_budget=self.project_budget,
                reason=f"Task cost ${self._current_task_cost:.2f} exceeds limit ${self.task_budget:.2f}",
            )

        # Check project budget
        if self._project_cost > self.project_budget:
            return BudgetStatus(
                within_budget=False,
                task_cost=self._current_task_cost,
                project_cost=self._project_cost,
                task_budget=self.task_budget,
                project_budget=self.project_budget,
                reason=f"Project cost ${self._project_cost:.2f} exceeds limit ${self.project_budget:.2f}",
            )

        # Check hourly burn rate
        if current_time and self._project_start_time:
            hours_elapsed = (current_time - self._project_start_time) / 3600
            if hours_elapsed > 0:
                hourly_rate = self._project_cost / hours_elapsed
                if hourly_rate > self.hourly_budget and self._project_cost > 5.0:
                    # Only trigger if we've spent at least $5 (avoid false alarms at start)
                    return BudgetStatus(
                        within_budget=False,
                        task_cost=self._current_task_cost,
                        project_cost=self._project_cost,
                        task_budget=self.task_budget,
                        project_budget=self.project_budget,
                        reason=f"Burn rate ${hourly_rate:.2f}/hr exceeds limit ${self.hourly_budget:.2f}/hr",
                    )

        return BudgetStatus(
            within_budget=True,
            task_cost=self._current_task_cost,
            project_cost=self._project_cost,
            task_budget=self.task_budget,
            project_budget=self.project_budget,
        )

    @property
    def total_cost(self) -> float:
        return self._project_cost
