"""v0.5.0 §4: crash-safe session journal.

The runtime journals each completed turn (flush + fsync) as it
happens instead of only saving in run()'s finally. A hard kill loses
at most the in-flight turn, which was never journaled and therefore
cannot duplicate on resume. A session whose meta still says
"running" is listed as interrupted and resumes from the journal.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from daemon.agent.runtime import AgentRuntime
from daemon.agent.session import Session, SessionMeta
from daemon.checkpoints import CheckpointManager
from daemon.errors import SessionError
from daemon.memory import MemoryStore
from daemon.permissions import PermissionEngine
from daemon.project import ProjectIndex
from daemon.providers.registry import AdapterRegistry
from daemon.tools import default_registry

REPO = Path(__file__).resolve().parents[2]


def _transcript_lines(session: Session) -> list[str]:
    path = session.directory / "transcript.jsonl"
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_journal_appends_incrementally_without_duplicates(home):
    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.add_user("first task")
    session.add_assistant("done")
    session.journal()
    assert len(_transcript_lines(session)) == 2

    session.add_user("second task")
    session.add_assistant("done too")
    session.journal()
    session.save()  # a later save must not re-append anything
    lines = _transcript_lines(session)
    assert len(lines) == 4

    loaded = Session.load(session.meta.id)
    assert [m.text for m in loaded.messages] == [
        "first task", "done", "second task", "done too"]
    assert loaded._journaled == 4


def test_save_twice_never_duplicates(home):
    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.add_user("hello")
    session.save()
    session.save()
    assert len(_transcript_lines(session)) == 1


def test_torn_final_journal_line_is_dropped(home):
    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.add_user("complete turn")
    session.add_assistant("answer")
    session.journal()
    # Simulate a kill landing mid-append: a partial final line.
    with open(session.directory / "transcript.jsonl", "a",
              encoding="utf-8") as handle:
        handle.write('{"role": "user", "text": "partial')
    loaded = Session.load(session.meta.id)
    assert [m.text for m in loaded.messages] == ["complete turn", "answer"]


def _append_raw(session: Session, *messages) -> None:
    from daemon.agent.session import redact_safe
    with open(session.directory / "transcript.jsonl", "a",
              encoding="utf-8") as handle:
        for message in messages:
            handle.write(json.dumps(redact_safe(message)) + "\n")


def test_kill_during_journal_write_leaves_no_partial_turn(home):
    # Review round 2, V2: round 1 journaled line-by-line, so a kill
    # after the assistant line but before the tool-results line left
    # a partial turn that load() accepted. Simulate exactly that torn
    # state: turn 2's user + assistant(tool call) lines are complete
    # and durable, the tool-results line is torn mid-write.
    from daemon.providers.types import Message, ToolCall

    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.add_user("first task")
    session.add_assistant("done")
    session.journal()

    turn2_user = Message.user("second task")
    turn2_assistant = Message.assistant(None, [ToolCall(
        id="c1", name="read_file", arguments={"path": "a.py"})])
    _append_raw(session, turn2_user, turn2_assistant)
    with open(session.directory / "transcript.jsonl", "a",
              encoding="utf-8") as handle:
        handle.write('{"role": "user", "tool_results": [{"tool_call_')

    loaded = Session.load(session.meta.id)
    # The partial turn leaves nothing loadable: no dangling tool call
    # is replayed. The completed turn and the user's second ask stay.
    assert [m.text for m in loaded.messages] == [
        "first task", "done", "second task"]
    assert all(not m.tool_calls for m in loaded.messages)


def test_partial_turn_with_no_torn_line_is_dropped(home):
    # The kill landed exactly between the assistant line landing and
    # the results line being written: every line present parses, the
    # turn is still partial and must be dropped.
    from daemon.providers.types import ToolCall

    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.add_user("only task")
    session.add_assistant(None, [ToolCall(
        id="c1", name="read_file", arguments={"path": "a.py"})])
    session.journal()

    loaded = Session.load(session.meta.id)
    assert [m.text for m in loaded.messages] == ["only task"]
    assert all(not m.tool_calls for m in loaded.messages)


def test_complete_tool_turn_survives_load(home):
    # The completeness check must not eat a whole turn: assistant
    # tool calls FOLLOWED by their results load intact.
    from daemon.providers.types import ToolCall, ToolResult

    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.add_user("read it")
    session.add_assistant(None, [ToolCall(
        id="c1", name="read_file", arguments={"path": "a.py"})])
    session.add_tool_results([ToolResult(
        tool_call_id="c1", tool_name="read_file", content="x = 1")])
    session.journal()

    loaded = Session.load(session.meta.id)
    assert [m.role for m in loaded.messages] == [
        "user", "assistant", "user"]
    assert loaded.messages[1].tool_calls[0].name == "read_file"
    assert loaded.messages[2].tool_results[0].content == "x = 1"


def test_corrupt_middle_line_still_raises(home):
    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.add_user("one")
    session.add_assistant("two")
    session.journal()
    path = session.directory / "transcript.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = "{not json"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(SessionError):
        Session.load(session.meta.id)


def test_completed_run_marks_status_complete(home, workspace):
    (workspace / "a.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    adapter = AdapterRegistry().create_by_id("demo")
    model = asyncio.run(adapter.list_models())[0]
    session = Session(meta=SessionMeta(workspace=str(workspace),
                                       provider="demo",
                                       model="demo-scripted"))
    index = ProjectIndex(workspace)
    index.build()
    runtime = AgentRuntime(
        adapter=adapter, model=model, registry=default_registry(),
        permissions=PermissionEngine(workspace, assume_yes=True),
        session=session, workspace=workspace,
        memory=MemoryStore(workspace),
        checkpoints=CheckpointManager(workspace), index=index)
    asyncio.run(runtime.run("add a docstring to a.py"))

    assert session.meta.status == "complete"
    on_disk = json.loads(
        (session.directory / "meta.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "complete"
    loaded = Session.load(session.meta.id)
    assert loaded.resumed_interrupted is False
    # The whole exchange is in the journal exactly once.
    assert len(_transcript_lines(session)) == len(session.messages)


_CHILD_SCRIPT = r'''
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])

from daemon.agent.runtime import AgentRuntime
from daemon.agent.session import Session, SessionMeta
from daemon.checkpoints import CheckpointManager
from daemon.memory import MemoryStore
from daemon.models import RemoteModel
from daemon.permissions import PermissionEngine
from daemon.project import ProjectIndex
from daemon.providers.types import ModelCapabilities, StreamEvent, ToolCall
from daemon.tools import default_registry


class Adapter:
    def __init__(self):
        self.calls = 0

    async def chat(self, request):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(kind="tool_calls", tool_calls=[
                ToolCall(id="c1", name="read_file",
                         arguments={"path": "a.py"})])
        else:
            await asyncio.sleep(3600)  # killed here, mid-turn
            yield StreamEvent(kind="text", text="never")

    async def close(self):
        pass


async def main():
    workspace = Path(sys.argv[1])
    session = Session(meta=SessionMeta(workspace=str(workspace),
                                       provider="mock", model="mock-coder"))
    print(session.meta.id, flush=True)
    index = ProjectIndex(workspace)
    index.build()
    runtime = AgentRuntime(
        adapter=Adapter(),
        model=RemoteModel(model_id="mock-coder", provider="mock",
                          capabilities=ModelCapabilities(
                              context_window=128000, tool_calling=True)),
        registry=default_registry(),
        permissions=PermissionEngine(workspace, assume_yes=True),
        session=session, workspace=workspace,
        memory=MemoryStore(workspace),
        checkpoints=CheckpointManager(workspace), index=index)
    await runtime.run("read a.py, then think forever")


asyncio.run(main())
'''


@pytest.mark.skipif(sys.platform == "win32",
                    reason="the kill test relies on POSIX SIGKILL")
def test_sigkill_mid_run_resumes_from_journal(tmp_path, monkeypatch):
    home = tmp_path / "daemon-home"
    home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    script = tmp_path / "kill_child.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    env = {**os.environ, "DAEMON_HOME": str(home),
           "NO_PROXY": "127.0.0.1,localhost",
           "no_proxy": "127.0.0.1,localhost"}
    proc = subprocess.Popen(
        [sys.executable, str(script), str(workspace), str(REPO)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env)
    try:
        session_id = proc.stdout.readline().strip()
        assert session_id
        transcript = (home / "sessions" / session_id
                      / "transcript.jsonl")
        # Wait until turn 1 (user + assistant + tool results) is
        # journaled, and the meta on disk says "running".
        deadline = time.time() + 60
        while time.time() < deadline:
            if transcript.exists() and len(
                    transcript.read_text(encoding="utf-8")
                    .splitlines()) >= 3:
                break
            if proc.poll() is not None:
                raise AssertionError(
                    f"child exited early: {proc.stderr.read()}")
            time.sleep(0.05)
        else:
            raise AssertionError("turn 1 was never journaled")
        meta = json.loads((home / "sessions" / session_id
                           / "meta.json").read_text(encoding="utf-8"))
        assert meta["status"] == "running"
        # V5: the running meta names its owning process and carries
        # a heartbeat, so a later listing can tell live from dead.
        assert meta["pid"] == proc.pid
        assert meta["heartbeat"] > 0
        proc.kill()  # SIGKILL: no finally, no save
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    monkeypatch.setenv("DAEMON_HOME", str(home))
    loaded = Session.load(session_id)
    # Exactly the completed turn - the in-flight turn (the sleeping
    # second model call) left nothing behind and nothing duplicated.
    assert [m.role for m in loaded.messages] == [
        "user", "assistant", "user"]
    assert loaded.messages[0].text == "read a.py, then think forever"
    assert loaded.messages[1].tool_calls[0].name == "read_file"
    assert loaded.messages[2].tool_results[0].tool_name == "read_file"
    assert loaded.resumed_interrupted is True
    assert loaded.meta.display_status == "interrupted"
    metas = Session.list_sessions()
    assert [m.display_status for m in metas if m.id == session_id] == [
        "interrupted"]


# ------------------------------------------------------------- V5: liveness


def test_live_session_is_not_reported_interrupted(home):
    # Review round 2, V5 (the reviewer's probe B): a session whose
    # owner is THIS still-alive process must not be listed as
    # interrupted - round 1 assumed the listing process is never the
    # owner, so two terminals were enough to corrupt the picture.
    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.meta.status = "running"
    session.meta.pid = os.getpid()
    session.add_user("in flight")
    session.save_meta()

    metas = Session.list_sessions()
    mine = [m for m in metas if m.id == session.meta.id]
    assert mine and mine[0].display_status == "running"
    loaded = Session.load(session.meta.id)
    assert loaded.resumed_interrupted is False


def test_dead_owner_reports_interrupted(home):
    # A pid that verifiably exited: interrupted, and load flags the
    # crash resume.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.meta.status = "running"
    session.meta.pid = proc.pid
    session.save_meta()

    assert session.meta.display_status == "interrupted"
    loaded = Session.load(session.meta.id)
    assert loaded.resumed_interrupted is True


def test_unknown_owner_reports_unknown_not_interrupted(home):
    # No pid recorded (a pre-V5 meta): liveness cannot be determined,
    # so the honest report is "unknown" - never a claimed interrupt.
    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.meta.status = "running"
    session.save_meta()

    assert session.meta.display_status == "unknown"
    loaded = Session.load(session.meta.id)
    assert loaded.resumed_interrupted is False


def test_journal_updates_heartbeat(home):
    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.add_user("beat")
    session.journal()
    on_disk = json.loads(
        (session.directory / "meta.json").read_text(encoding="utf-8"))
    assert on_disk["heartbeat"] > 0
    assert session.meta.heartbeat > 0


def test_second_concurrent_resume_is_refused(home):
    # Review round 2, V5 (the reviewer's probe C): two resumes of
    # one session interleaved appends and duplicated a turn. The
    # resume lock makes the second resume fail with a clear message;
    # releasing the first lock frees the session again.
    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.add_user("turn one")
    session.journal()

    first = Session.load(session.meta.id, lock=True)
    try:
        with pytest.raises(SessionError, match="already in use"):
            Session.load(session.meta.id, lock=True)
    finally:
        first.release_lock()
    second = Session.load(session.meta.id, lock=True)
    second.release_lock()


# ------------------------------------------------- R2-5: lock + heartbeat


def _plant_meta(session, *, heartbeat: float) -> None:
    """Rewrite the on-disk meta's heartbeat (save_meta refreshes it,
    so a stale one has to be planted directly, as a crashed owner's
    last write would leave it)."""
    meta_path = session.directory / "meta.json"
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    data["heartbeat"] = heartbeat
    meta_path.write_text(json.dumps(data), encoding="utf-8")


def _listed_meta(session_id: str) -> SessionMeta:
    metas = [m for m in Session.list_sessions() if m.id == session_id]
    assert metas
    return metas[0]


def test_recycled_pid_with_stale_heartbeat_is_not_running(home):
    # Review round 3, R2-5 (the reviewer's repro): the recorded pid
    # now belongs to a live UNRELATED process (pid recycling), the
    # heartbeat is 3 hours stale, and no resume lock is held. Round
    # 2 trusted the pid alone and reported "running" indefinitely.
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        session = Session(meta=SessionMeta(workspace="/tmp"))
        session.meta.status = "running"
        session.meta.pid = sleeper.pid
        session.save_meta()
        _plant_meta(session, heartbeat=time.time() - 3 * 3600)

        assert _listed_meta(session.meta.id).display_status == \
            "interrupted"
        loaded = Session.load(session.meta.id)
        assert loaded.resumed_interrupted is True
    finally:
        sleeper.kill()
        sleeper.wait(timeout=30)


def test_held_lock_reports_running_despite_stale_heartbeat(home):
    # The lock is the PRIMARY signal (R2-5): while the owner holds
    # it, the session is running even if the heartbeat is old (one
    # long model call journals nothing) and the recorded pid is
    # already gone from a listing's point of view.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)  # a verifiably dead pid
    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.meta.status = "running"
    session.meta.pid = proc.pid
    session.add_user("in flight")
    session.journal()
    _plant_meta(session, heartbeat=time.time() - 3 * 3600)

    holder = Session.load(session.meta.id, lock=True)
    try:
        assert _listed_meta(session.meta.id).display_status == \
            "running"
    finally:
        holder.release_lock()
    # Lock released + dead pid: the truth comes out.
    assert _listed_meta(session.meta.id).display_status == \
        "interrupted"


# ------------------------------------------------- R3-3: lockless owner


def test_lockless_live_owner_with_stale_heartbeat_is_unknown(
        home, monkeypatch):
    # Review round 4, R3-3 (the reviewer's live-pid repro): the
    # owner is LOCKLESS - on a platform with neither fcntl nor
    # msvcrt, acquire_lock() silently no-ops, so no lock state
    # exists to consult. Its pid is alive (this process), but one
    # model call longer than the 600s heartbeat threshold journals
    # nothing, so the heartbeat is stale. Round 3's recycled-pid
    # inference reported this live owner "interrupted" and a resume
    # flagged it as a crash. "interrupted" requires positive
    # evidence (pid dead, or lock free AND pid dead); a live pid
    # with an indeterminate lock state is "unknown".
    from daemon.agent import session as session_module

    monkeypatch.setattr(session_module, "fcntl", None)
    monkeypatch.setattr(session_module, "msvcrt", None)
    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.meta.status = "running"
    session.meta.pid = os.getpid()
    session.save_meta()
    _plant_meta(session, heartbeat=time.time() - 601)

    assert _listed_meta(session.meta.id).display_status == "unknown"
    loaded = Session.load(session.meta.id)
    assert loaded.resumed_interrupted is False
    # A FRESH heartbeat still proves the lockless owner live: the
    # vocabulary change is about the stale + indeterminate case,
    # not about lockless owners in general.
    _plant_meta(session, heartbeat=time.time())
    assert _listed_meta(session.meta.id).display_status == "running"


def test_unopenable_lock_path_with_stale_heartbeat_is_unknown(home):
    # R3-3, the other lockless construction: lock acquisition
    # failed at run start because session.lock is a DIRECTORY, so
    # the lock state cannot be probed (the probe's open fails).
    # Live pid + stale heartbeat + indeterminate lock: "unknown",
    # never "interrupted".
    session = Session(meta=SessionMeta(workspace="/tmp"))
    session.meta.status = "running"
    session.meta.pid = os.getpid()
    session.save_meta()
    (session.directory / "session.lock").mkdir()
    _plant_meta(session, heartbeat=time.time() - 601)

    assert _listed_meta(session.meta.id).display_status == "unknown"
    loaded = Session.load(session.meta.id)
    assert loaded.resumed_interrupted is False
