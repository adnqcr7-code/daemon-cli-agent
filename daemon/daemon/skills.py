"""Agent skills: reusable markdown instructions loaded on demand.

A skill is a small markdown file (user-level ``~/.daemon/skills/`` or
project-level ``.daemon/skills/``) with a simple header::

    name: pytest-debugging
    description: Systematic approach to debugging pytest failures
    tags: testing, pytest

    Body: instructions the agent should follow when the skill applies.

Skills are never auto-executed; the user (or the model, with the user's
approval) attaches them to the session. Their content is data appended to
the system prompt - it cannot override security policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Skill:
    name: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    body: str = ""
    origin: str = "user"

    def header(self) -> str:
        return f"skill '{self.name}' ({', '.join(self.tags) or 'general'})"


_HEADER_KEYS = ("name", "description", "tags")


class SkillLibrary:
    def __init__(self, *, user_dir: Path, project_dir: Path | None) -> None:
        self.user_dir = Path(user_dir)
        self.project_dir = Path(project_dir) if project_dir else None

    def skills(self) -> list[Skill]:
        found: dict[str, Skill] = {}
        # User skills first so project skills (iterated last) win on clash.
        for directory, origin in ((self.user_dir, "user"),
                                  (self.project_dir, "project")):
            if directory is None or not directory.exists():
                continue
            for path in sorted(directory.glob("*.md")):
                skill = _parse_skill(path, origin)
                if skill is not None:
                    found[skill.name] = skill
        return list(found.values())

    def get(self, name: str) -> Skill | None:
        for skill in self.skills():
            if skill.name == name:
                return skill
        return None

    def match(self, query: str) -> list[Skill]:
        terms = [term for term in re.split(r"\W+", query.lower()) if term]
        scored: list[tuple[int, Skill]] = []
        for skill in self.skills():
            haystack = (skill.name + " " + skill.description + " " +
                        " ".join(skill.tags)).lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, skill))
        scored.sort(key=lambda pair: -pair[0])
        return [skill for _, skill in scored]


def _parse_skill(path: Path, origin: str) -> Skill | None:
    try:
        text = path.read_text("utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    header: dict[str, str] = {}
    index = 0
    if lines and lines[0].strip() == "---":
        index = 1
        while index < len(lines) and lines[index].strip() != "---":
            match = re.match(r"^(\w[\w-]*):\s*(.*)$", lines[index])
            if match:
                header[match.group(1).lower()] = match.group(2).strip()
            index += 1
        index += 1
    body = "\n".join(lines[index:]).strip()
    if not body:
        # Header-only skills are still valid: the description is the content.
        body = header.get("description", "")
    if not body:
        return None
    return Skill(
        name=header.get("name") or path.stem,
        description=header.get("description", ""),
        tags=[tag.strip() for tag in header.get("tags", "").split(",")
              if tag.strip()],
        body=body,
        origin=origin,
    )
