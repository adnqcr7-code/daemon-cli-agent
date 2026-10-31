"""Planning: structured plan parsing and task tracking.

The planner asks the model for strict JSON; parsing is defensive (fenced
JSON, trailing commas, prose around the object) but never fabricates tasks
when parsing fails - the user gets the raw output and an error instead.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..log import get_logger

_logger = get_logger("planner")

STATUSES = ("pending", "in_progress", "done", "failed", "blocked")


@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    files: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    verification: str = ""
    risk: str = "low"
    status: str = "pending"
    assigned: str = ""
    result: str = ""


@dataclass
class Plan:
    goal: str
    summary: str = ""
    tasks: list[Task] = field(default_factory=list)

    def task(self, task_id: str) -> Task | None:
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def ordered(self) -> list[Task]:
        """Topologically ordered tasks (dependencies first)."""
        order: list[Task] = []
        resolved: set[str] = set()

        def visit(task: Task, stack: tuple[str, ...] = ()) -> None:
            if task.id in resolved or task.id in stack:
                order.append(task) if task.id not in resolved else None
                resolved.add(task.id)
                return
            for dep in task.depends_on:
                dependency = self.task(dep)
                if dependency is not None:
                    visit(dependency, stack + (task.id,))
            if task.id not in resolved:
                order.append(task)
                resolved.add(task.id)

        for task in self.tasks:
            visit(task)
        return order

    def next_task(self) -> Task | None:
        for task in self.ordered():
            if task.status == "pending":
                return task
        return None


def parse_plan(goal: str, model_output: str) -> Plan:
    """Parse the model's plan JSON; raises ValueError with context on failure."""
    payload = _extract_json(model_output)
    if payload is None:
        raise ValueError(
            "The planner did not return parseable JSON. Raw output:\n" +
            model_output[:800])
    tasks: list[Task] = []
    for index, raw in enumerate(payload.get("tasks") or [], start=1):
        if not isinstance(raw, dict):
            continue
        task_id = str(raw.get("id") or index)
        tasks.append(Task(
            id=task_id,
            title=str(raw.get("title") or f"Task {task_id}"),
            description=str(raw.get("description") or ""),
            files=[str(file) for file in (raw.get("files") or [])][:10],
            depends_on=[str(dep) for dep in (raw.get("depends_on") or [])][:5],
            verification=str(raw.get("verification") or ""),
            risk=str(raw.get("risk") or "low"),
        ))
    if not tasks:
        raise ValueError("The plan contains no tasks.")
    return Plan(goal=goal, summary=str(payload.get("summary") or ""), tasks=tasks)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of possibly-fenced, prose-wrapped text."""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    candidates = [cleaned]
    brace = cleaned.find("{")
    if brace > 0:
        candidates.append(cleaned[brace:])
    for candidate in candidates:
        for attempt in (candidate, _strip_trailing_commas(candidate)):
            if not attempt.startswith("{"):
                continue
            try:
                payload = json.loads(attempt)
                if isinstance(payload, dict):
                    return payload
            except json.JSONDecodeError:
                continue
    return None


def _strip_trailing_commas(text: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", text)


class TaskTracker:
    """Mutable task state shown in the UI and injected into prompts."""

    def __init__(self) -> None:
        self.plan: Plan | None = None

    def load(self, plan: Plan) -> None:
        self.plan = plan

    def start(self, task: Task) -> None:
        task.status = "in_progress"

    def finish(self, task: Task, *, success: bool, result: str = "") -> None:
        task.status = "done" if success else "failed"
        task.result = result[:500]

    def render(self) -> str:
        if self.plan is None:
            return "No active plan."
        lines = [f"Plan: {self.plan.goal}",
                 self.plan.summary or "", "Tasks:"]
        for task in self.plan.ordered():
            mark = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]",
                    "failed": "[!]", "blocked": "[-]"}[task.status]
            lines.append(f"  {mark} {task.id}. {task.title}"
                         + (f" ({task.files and ', '.join(task.files) or ''})"
                            if task.files else ""))
            if task.result:
                lines.append(f"      -> {task.result.splitlines()[0]}")
        done = sum(1 for task in self.plan.tasks if task.status == "done")
        lines.append(f"Progress: {done}/{len(self.plan.tasks)} done")
        return "\n".join(lines)

    def context_block(self) -> str:
        """Compact block for the system prompt during plan execution."""
        if self.plan is None:
            return ""
        current = self.plan.next_task()
        lines = ["You are executing an approved task plan:"]
        for task in self.plan.ordered():
            state = {"pending": "todo", "in_progress": "CURRENT",
                     "done": "done", "failed": "failed",
                     "blocked": "blocked"}[task.status]
            lines.append(f"- [{state}] {task.id}. {task.title}"
                         + (f" | files: {', '.join(task.files)}" if task.files else "")
                         + (f" | verify: {task.verification}" if task.verification else ""))
        if current is not None:
            lines.append(f"Work on the CURRENT task next: {current.id}. "
                         f"{current.description}")
        return "\n".join(lines)
