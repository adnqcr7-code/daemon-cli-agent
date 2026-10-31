"""Memory layers: add, search, remove, project context, redaction."""

from daemon.memory import MemoryStore


def test_add_and_search(home, workspace):
    store = MemoryStore(workspace, home=home)
    store.add("Use pytest for all new tests", kind="decision", tags=["testing"])
    store.add("The API base URL is v2", kind="fact")
    store.add("Node 20 is the runtime", kind="fact", scope="user")
    hits = store.search("pytest")
    assert len(hits) == 1 and "pytest" in hits[0].content
    assert store.search("nothing-matches") == []


def test_search_ranks_by_term_overlap(home, workspace):
    store = MemoryStore(workspace, home=home)
    store.add("database uses sqlite", kind="fact")
    store.add("sqlite migrations live in db/sqlite.py", kind="fact")
    hits = store.search("sqlite migrations")
    assert hits[0].content.startswith("sqlite migrations")


def test_scopes_are_separate(home, workspace, tmp_path):
    other = tmp_path / "other-workspace"
    other.mkdir()
    store = MemoryStore(workspace, home=home)
    store.add("project note", scope="project")
    store.add("user note", scope="user")
    other_store = MemoryStore(other, home=home)
    # user memory shared, project memory not
    assert len(other_store.entries()) == 1
    assert other_store.entries()[0].content == "user note"


def test_remove_by_id(home, workspace):
    store = MemoryStore(workspace, home=home)
    entry = store.add("temporary note")
    assert store.remove(entry.id)
    assert store.search("temporary note") == []
    assert not store.remove(entry.id)


def test_clear(home, workspace):
    store = MemoryStore(workspace, home=home)
    store.add("a", scope="project")
    store.add("b", scope="project")
    store.add("c", scope="user")
    assert store.clear("project") == 2
    assert len(store.entries()) == 1


def test_secrets_are_redacted_before_storage(home, workspace):
    from daemon.security import redactor

    redactor().register("sk-live-secret-key-9999")
    store = MemoryStore(workspace, home=home)
    store.add("the key is sk-live-secret-key-9999 ok")
    assert "sk-live-secret-key-9999" not in (
        workspace / ".daemon" / "memory.jsonl").read_text()


def test_project_context_blocks(home, workspace):
    store = MemoryStore(workspace, home=home)
    store.add("Decision: prefer composition", kind="decision")
    store.add("Fact: python 3.12", kind="fact")
    block = store.project_context()
    assert "Engineering decisions" in block
    assert "prefer composition" in block


def test_empty_project_context(home, workspace):
    assert MemoryStore(workspace, home=home).project_context() == ""
