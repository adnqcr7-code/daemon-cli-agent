"""Verified memory: backward compat, add_verified, invalidate, contradictions."""

import json

from daemon.memory import MemoryEntry, MemoryStore


def test_old_format_jsonl_loads_with_defaults(home, workspace):
    store = MemoryStore(workspace, home=home)
    store.project_path.parent.mkdir(parents=True, exist_ok=True)
    old = {"content": "legacy fact", "kind": "fact", "tags": ["old"],
           "source": "user", "scope": "project", "id": "old123",
           "created": 1700000000.0}
    store.project_path.write_text(json.dumps(old) + "\n", encoding="utf-8")
    entries = store.entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.content == "legacy fact"
    assert entry.confidence == 1.0
    assert entry.source_location == ""
    assert entry.verified is False
    assert entry.supersedes == []
    assert entry.invalidated is False
    assert entry.invalidated_reason == ""


def test_memory_entry_from_json_defaults():
    entry = MemoryEntry.from_json({"content": "plain"})
    assert entry.confidence == 1.0
    assert entry.verified is False
    assert entry.invalidated is False


def test_add_verified_round_trips_all_fields(home, workspace):
    store = MemoryStore(workspace, home=home)
    entry = store.add_verified(
        "API base URL is v2", source="agent", source_location="api.py:42",
        confidence=0.85, tags=["api"], supersedes=["old123"])
    assert entry.kind == "verified_fact"
    assert entry.verified is True
    assert entry.scope == "project"
    assert entry.confidence == 0.85
    assert entry.source_location == "api.py:42"
    assert entry.supersedes == ["old123"]
    assert entry.invalidated is False
    loaded = [item for item in store.entries() if item.id == entry.id]
    assert len(loaded) == 1
    reloaded = loaded[0]
    assert reloaded.content == "API base URL is v2"
    assert reloaded.kind == "verified_fact"
    assert reloaded.source == "agent"
    assert reloaded.tags == ["api"]
    assert reloaded.confidence == 0.85
    assert reloaded.source_location == "api.py:42"
    assert reloaded.verified is True
    assert reloaded.supersedes == ["old123"]
    # Round-trip through to_json/from_json preserves the new fields too.
    again = MemoryEntry.from_json(reloaded.to_json())
    assert again == reloaded


def test_add_verified_clamps_confidence(home, workspace):
    store = MemoryStore(workspace, home=home)
    high = store.add_verified("too confident", source="user",
                              source_location="a.py:1", confidence=1.5)
    low = store.add_verified("not confident", source="user",
                             source_location="a.py:2", confidence=-0.5)
    assert high.confidence == 1.0
    assert low.confidence == 0.0
    loaded = {entry.id: entry for entry in store.entries()}
    assert loaded[high.id].confidence == 1.0
    assert loaded[low.id].confidence == 0.0


def test_invalidate_hides_entry_but_does_not_delete(home, workspace):
    store = MemoryStore(workspace, home=home)
    entry = store.add_verified("uniquezephyr fact", source="user",
                               source_location="z.py:1", confidence=0.9)
    assert store.invalidate(entry.id, "superseded by v3")
    assert store.entries() == []
    assert store.search("uniquezephyr") == []
    assert "uniquezephyr" not in store.project_context()
    assert store.get_verified_facts() == []
    shown = store.entries(include_invalidated=True)
    assert len(shown) == 1
    assert shown[0].invalidated is True
    assert shown[0].invalidated_reason == "superseded by v3"
    # The line is still on disk, marked rather than deleted.
    lines = store.project_path.read_text("utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["invalidated"] is True


def test_invalidate_unknown_id_returns_false(home, workspace):
    store = MemoryStore(workspace, home=home)
    store.add("some note")
    assert store.invalidate("does-not-exist", "nope") is False


def test_find_contradictions_same_source_location(home, workspace):
    store = MemoryStore(workspace, home=home)
    existing = store.add_verified("timeout is 30 seconds", source="agent",
                                  source_location="config.py:10")
    store.add_verified("unrelated fact", source="agent",
                       source_location="other.py:1")
    hits = store.find_contradictions("timeout is 60 seconds",
                                     source_location="config.py:10")
    assert [entry.id for entry in hits] == [existing.id]
    # Identical content at the same location is not a contradiction.
    assert store.find_contradictions("timeout is 30 seconds",
                                     source_location="config.py:10") == []
    # A different location does not flag the existing fact.
    assert store.find_contradictions("timeout is 60 seconds",
                                     source_location="elsewhere.py:9") == []


def test_find_contradictions_token_overlap_without_location(home, workspace):
    store = MemoryStore(workspace, home=home)
    existing = store.add_verified("database uses sqlite for storage",
                                  source="agent", source_location="db.py:1")
    hits = store.find_contradictions("database uses sqlite for storage layer")
    assert [entry.id for entry in hits] == [existing.id]
    assert store.find_contradictions("completely different topic words") == []


def test_get_verified_facts_ordering_and_excludes_invalidated(home, workspace):
    store = MemoryStore(workspace, home=home)
    low = store.add_verified("low confidence", source="user",
                             source_location="a.py:1", confidence=0.5)
    high = store.add_verified("high confidence", source="user",
                              source_location="a.py:2", confidence=0.95)
    mid = store.add_verified("mid confidence", source="user",
                             source_location="a.py:3", confidence=0.95)
    store.add("ordinary note", kind="fact")
    facts = store.get_verified_facts()
    # Confidence desc, then newest first within the same confidence.
    assert [entry.id for entry in facts] == [mid.id, high.id, low.id]
    assert store.get_verified_facts(limit=1)[0].id == mid.id
    assert store.invalidate(high.id, "stale")
    assert [entry.id for entry in store.get_verified_facts()] == [mid.id, low.id]


def test_project_context_shows_verified_block_first(home, workspace):
    store = MemoryStore(workspace, home=home)
    store.add("Decision: prefer composition", kind="decision")
    store.add_verified("API base URL is v2", source="agent",
                       source_location="api.py:42", confidence=0.9)
    block = store.project_context()
    assert block.startswith("Verified facts:")
    assert "- API base URL is v2 (source: api.py:42, confidence 0.90)" in block
    assert "Engineering decisions:" in block
    assert block.index("Verified facts:") < block.index("Engineering decisions:")


def test_project_context_verified_block_falls_back_to_source(home, workspace):
    store = MemoryStore(workspace, home=home)
    store.add_verified("no location fact", source="tester", source_location="")
    block = store.project_context()
    assert "(source: tester, confidence 0.90)" in block
