"""Themes, skills, planner parsing and session persistence."""

import asyncio

from daemon.skills import SkillLibrary


# ------------------------------------------------------------- themes


def test_theme_registry_has_all_four():
    from daemon.ui.themes import themes

    names = {theme.name for theme in themes()}
    assert names == {"obsidian", "archive", "minimal", "classic"}


def test_obsidian_is_violet_cyan():
    from daemon.ui.themes import OBSIDIAN

    assert "a78bfa" in OBSIDIAN.styles["accent"]
    assert "22d3ee" in OBSIDIAN.styles["accent2"]


def test_archive_is_distinct_from_obsidian():
    from daemon.ui.themes import ARCHIVE, OBSIDIAN

    assert ARCHIVE.styles["accent"] != OBSIDIAN.styles["accent"]
    assert ARCHIVE.styles["accent2"] != OBSIDIAN.styles["accent2"]


def test_no_color_degrades_to_minimal(monkeypatch):
    from daemon.ui.themes import MINIMAL, resolve_theme

    monkeypatch.setenv("NO_COLOR", "1")
    assert resolve_theme("obsidian").name == MINIMAL.name


def test_get_theme_falls_back_to_obsidian():
    from daemon.ui.themes import get_theme

    assert get_theme("does-not-exist").name == "obsidian"


# --------------------------------------------------------------- skills


def test_skill_parsing(tmp_path):
    user = tmp_path / "skills"
    user.mkdir()
    (user / "pytest.md").write_text(
        "---\nname: pytest-debugging\ndescription: Debug failing tests\n"
        "tags: testing, pytest\n---\n\nRead the traceback bottom-up...")
    library = SkillLibrary(user_dir=user, project_dir=None)
    skills = library.skills()
    assert len(skills) == 1
    skill = skills[0]
    assert skill.name == "pytest-debugging"
    assert "testing" in skill.tags
    assert skill.body.startswith("Read the traceback")


def test_skill_without_header_uses_filename(tmp_path):
    user = tmp_path / "skills"
    user.mkdir()
    (user / "deploy.md").write_text("Deployment steps here")
    library = SkillLibrary(user_dir=user, project_dir=None)
    assert library.get("deploy").body == "Deployment steps here"


def test_skill_match_by_keywords(tmp_path):
    user = tmp_path / "skills"
    user.mkdir()
    (user / "a.md").write_text("---\ndescription: database migrations\n---\n")
    (user / "b.md").write_text("---\ndescription: css styling\n---\n")
    library = SkillLibrary(user_dir=user, project_dir=None)
    matches = library.match("how do I run database migrations")
    assert matches and matches[0].name == "a"


def test_project_skills_override_user(tmp_path):
    user = tmp_path / "user-skills"
    user.mkdir()
    (user / "x.md").write_text("---\nname: shared\n---\nuser version")
    project = tmp_path / "project" / ".daemon" / "skills"
    project.mkdir(parents=True)
    (project / "y.md").write_text("---\nname: shared\n---\nproject version")
    library = SkillLibrary(user_dir=user, project_dir=project)
    assert library.get("shared").body == "project version"


# --------------------------------------------------------------- planner


def test_parse_plan_strict_json():
    from daemon.agent.planner import parse_plan

    payload = ('{"summary": "fix bug", "tasks": ['
               '{"id": "1", "title": "Read code", "files": ["a.py"],'
               ' "verification": "pytest", "depends_on": []},'
               '{"id": "2", "title": "Fix bug", "depends_on": ["1"]}]}')
    plan = parse_plan("goal", payload)
    assert plan.summary == "fix bug"
    assert plan.ordered()[0].title == "Read code"
    assert plan.ordered()[1].depends_on == ["1"]


def test_parse_plan_tolerates_fences_and_prose():
    from daemon.agent.planner import parse_plan

    payload = ('Sure! Here is the plan:\n```json\n'
               '{"summary": "s", "tasks": [{"id": "1", "title": "T"}]}\n'
               '```\nHope that helps.')
    plan = parse_plan("goal", payload)
    assert plan.tasks[0].title == "T"


def test_parse_plan_rejects_garbage():
    import pytest

    from daemon.agent.planner import parse_plan

    with pytest.raises(ValueError):
        parse_plan("goal", "I could not produce a plan sorry")


def test_task_tracker_render_and_context():
    from daemon.agent.planner import TaskTracker, parse_plan

    plan = parse_plan("goal", '{"summary": "", "tasks": ['
                      '{"id": "1", "title": "First"},'
                      '{"id": "2", "title": "Second"}]}')
    tracker = TaskTracker()
    tracker.load(plan)
    first = plan.next_task()
    tracker.start(first)
    tracker.finish(first, success=True, result="done ok")
    rendered = tracker.render()
    assert "[x] 1. First" in rendered
    assert "[ ] 2. Second" in rendered
    assert "1/2 done" in rendered
    block = tracker.context_block()
    assert "CURRENT" in block


# --------------------------------------------------------------- sessions


def test_session_save_load_roundtrip(home):
    from daemon.agent.session import Session
    from daemon.providers.types import ToolCall, ToolResult

    session = Session()
    session.meta.workspace = "/tmp/somewhere"
    session.add_user("hello")
    session.add_assistant("hi", [ToolCall(id="1", name="read_file",
                                          arguments={"path": "x"})])
    session.add_tool_results([ToolResult(tool_call_id="1",
                                         tool_name="read_file",
                                         content="file body")])
    session.save()
    loaded = Session.load(session.meta.id)
    assert loaded.meta.id == session.meta.id
    assert len(loaded.messages) == 3
    assert loaded.messages[1].tool_calls[0].name == "read_file"
    assert loaded.messages[2].tool_results[0].content == "file body"


def test_session_redacts_secrets_in_transcript(home):
    from daemon.agent.session import Session
    from daemon.security import redactor

    redactor().register("sk-session-secret-0000")
    session = Session()
    session.add_user("here is sk-session-secret-0000")
    session.save()
    import json

    lines = (home / "sessions" / session.meta.id /
             "transcript.jsonl").read_text().splitlines()
    assert "sk-session-secret-0000" not in json.dumps(lines)


def test_session_list_and_delete(home):
    from daemon.agent.session import Session

    session = Session()
    session.add_user("q")
    session.save()
    assert any(meta.id == session.meta.id for meta in Session.list_sessions())
    Session.delete(session.meta.id)
    assert not any(meta.id == session.meta.id
                   for meta in Session.list_sessions())


def test_compaction_rewrite(home):
    from daemon.agent.session import Session

    session = Session()
    for index in range(10):
        session.add_user(f"turn {index}")
    session.save()
    session.messages = session.messages[-2:]
    session.rewrite_transcript()
    loaded = Session.load(session.meta.id)
    assert len(loaded.messages) == 2
