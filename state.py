"""State management for crash recovery and progress tracking."""
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from config import STATE_FILE


@dataclass
class TaskState:
    id: str
    name: str
    status: str = "pending"  # pending | running | completed | failed | skipped
    attempts: int = 0
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None
    claude_session_id: Optional[str] = None
    output_log: Optional[str] = None
    tokens_used: int = 0
    cost_usd: float = 0.0


@dataclass
class ProjectState:
    project_name: str = ""
    task_yaml_path: str = ""
    repo_path: str = ""
    tasks: dict[str, TaskState] = field(default_factory=dict)
    task_order: list[str] = field(default_factory=list)
    current_task_id: Optional[str] = None
    status: str = "idle"  # idle | running | paused | completed | failed
    started_at: Optional[float] = None
    injected_instructions: list[str] = field(default_factory=list)
    total_tokens: int = 0
    total_cost: float = 0.0

    def save(self):
        """Persist state to disk for crash recovery."""
        data = {
            "project_name": self.project_name,
            "task_yaml_path": self.task_yaml_path,
            "repo_path": self.repo_path,
            "tasks": {tid: asdict(ts) for tid, ts in self.tasks.items()},
            "task_order": self.task_order,
            "current_task_id": self.current_task_id,
            "status": self.status,
            "started_at": self.started_at,
            "injected_instructions": self.injected_instructions,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
        }
        STATE_FILE.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls) -> Optional["ProjectState"]:
        """Load state from disk."""
        if not STATE_FILE.exists():
            return None
        try:
            data = json.loads(STATE_FILE.read_text())
            state = cls(
                project_name=data["project_name"],
                task_yaml_path=data["task_yaml_path"],
                repo_path=data["repo_path"],
                task_order=data["task_order"],
                current_task_id=data.get("current_task_id"),
                status=data["status"],
                started_at=data.get("started_at"),
                injected_instructions=data.get("injected_instructions", []),
                total_tokens=data.get("total_tokens", 0),
                total_cost=data.get("total_cost", 0.0),
            )
            for tid, ts_data in data["tasks"].items():
                state.tasks[tid] = TaskState(**ts_data)
            return state
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[state] Failed to load state: {e}")
            return None

    def get_current_task(self) -> Optional[TaskState]:
        if self.current_task_id and self.current_task_id in self.tasks:
            return self.tasks[self.current_task_id]
        return None

    def get_next_task(self) -> Optional[TaskState]:
        """Get next pending task respecting dependency order."""
        for tid in self.task_order:
            ts = self.tasks[tid]
            if ts.status == "pending":
                return ts
        return None

    def count_by_status(self) -> dict[str, int]:
        counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "skipped": 0}
        for ts in self.tasks.values():
            counts[ts.status] = counts.get(ts.status, 0) + 1
        return counts

    def progress_bar(self, width: int = 20) -> str:
        total = len(self.tasks)
        if total == 0:
            return "[" + "░" * width + "] 0%"
        done = sum(1 for ts in self.tasks.values() if ts.status in ("completed", "skipped"))
        pct = done / total
        filled = int(pct * width)
        bar = "█" * filled + "░" * (width - filled)
        return f"[{bar}] {done}/{total} ({pct:.0%})"

    def mark_task_running(self, task_id: str):
        self.tasks[task_id].status = "running"
        self.tasks[task_id].started_at = time.time()
        self.tasks[task_id].attempts += 1
        self.current_task_id = task_id
        self.status = "running"
        self.save()

    def mark_task_completed(self, task_id: str, tokens: int = 0, cost: float = 0.0):
        ts = self.tasks[task_id]
        ts.status = "completed"
        ts.completed_at = time.time()
        ts.tokens_used += tokens
        ts.cost_usd += cost
        self.total_tokens += tokens
        self.total_cost += cost
        self.current_task_id = None
        self.save()

    def mark_task_failed(self, task_id: str, error: str):
        ts = self.tasks[task_id]
        ts.status = "failed"
        ts.error = error
        self.current_task_id = None
        self.save()

    def reset_task(self, task_id: str):
        ts = self.tasks[task_id]
        ts.status = "pending"
        ts.error = None
        self.save()
