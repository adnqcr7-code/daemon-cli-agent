"""BITON design references: catalogue parsing, search, briefs and tools."""

import asyncio

import pytest

from daemon.designs import (BitonClient, Design, DesignError,
                            extract_catalogue, render_brief)
from daemon.tools import ToolContext, ToolRegistry
from daemon.tools.design import register_design_tools

HTML = """<html><body>
<script>
/* ==================== DATA ==================== */
var DESIGNS = [
{id:"northwind-bank",title:"Northwind Bank",app:"Northwind Bank",
 cat:"Mobile Apps",sub:"Banking",platform:["iOS"],style:"Minimal",
 views:48200,saves:3910,screens:6,tags:"banking finance",
 pal:{bg:"#F4F1EA",ink:"#14231C",accent:"#1E7A4F",soft:"#DCE8DF"},
 desc:"A calm banking home, one number up front.",
 flow:["Home","Accounts","Send money"], type:{display:"Söhne",body:"Söhne",
 scale:"28 / 20 / 16 / 13",note:"Tabular numerals."},
 spacing:"8pt grid, 20pt margins.",
 comps:["Balance card","Account row"],
 resp:"Single column; left rail on tablets.",
 blob:"AAAA""" + "x" * 5000 + """",
 trailer:null},
{id:"atlas-coast",title:"Atlas Coast",cat:"Websites",sub:"Travel",
 platform:["Web"],style:"Editorial",views:100,flow:["Landing"],
 comps:["Hero gallery"],tags:"travel coastal",
 liveUrl:"https://example.test/atlas-coast"}
];
</script></body></html>"""


def _client(monkeypatch, *, html: str = HTML, ttl_s: int = 3600,
            calls: list | None = None) -> BitonClient:
    async def fake_fetch(self):
        if calls is not None:
            calls.append(1)
        return html

    monkeypatch.setattr(BitonClient, "_fetch_html", fake_fetch)
    return BitonClient(ttl_s=ttl_s)


def test_parse_catalogue_handles_js_literals():
    entries = extract_catalogue(HTML)
    assert [entry["id"] for entry in entries] == ["northwind-bank",
                                                  "atlas-coast"]
    assert entries[0]["pal"]["accent"] == "#1E7A4F"
    assert entries[0]["type"]["display"] == "Söhne"
    assert entries[1]["liveUrl"].endswith("/atlas-coast")


def test_parse_catalogue_rejects_pages_without_data():
    with pytest.raises(DesignError):
        extract_catalogue("<html>nothing here</html>")


def test_cache_is_reused_and_refresh_refetches(monkeypatch):
    calls: list = []
    client = _client(monkeypatch, calls=calls)

    async def go():
        first = await client.designs()
        second = await client.designs()
        await client.designs(refresh=True)
        return first, second

    first, second = asyncio.run(go())
    assert len(first) == 2 and len(second) == 2
    assert len(calls) == 2  # cached read, then one forced refresh
    assert first[0].slug == "northwind-bank"


def test_cache_drops_embedded_images(monkeypatch):
    client = _client(monkeypatch)

    async def go():
        return await client.get("northwind-bank")

    design = asyncio.run(go())
    assert "blob" not in design.data
    assert "trailer" not in design.data


def test_get_accepts_slug_hash_and_path_urls(monkeypatch):
    client = _client(monkeypatch)

    async def go():
        return [await client.get(ref) for ref in (
            "northwind-bank",
            "https://biton-eta.vercel.app/#/design/northwind-bank",
            "https://biton-eta.vercel.app/designs/northwind-bank/")]

    designs = asyncio.run(go())
    assert {design.slug for design in designs} == {"northwind-bank"}


def test_get_unknown_design_suggests_matches(monkeypatch):
    client = _client(monkeypatch)

    async def go():
        return await client.get("northwind-banks")

    with pytest.raises(DesignError) as excinfo:
        asyncio.run(go())
    assert "northwind-bank" in (excinfo.value.hint or "")


def test_search_filters_and_ranking(monkeypatch):
    client = _client(monkeypatch)

    async def go():
        banking = await client.search("banking")
        web = await client.search("", platform="Web")
        live = await client.search("", live=True)
        missing = await client.search("ziggurat")
        return banking, web, live, missing

    banking, web, live, missing = asyncio.run(go())
    assert [design.slug for design in banking] == ["northwind-bank"]
    assert [design.slug for design in web] == ["atlas-coast"]
    assert [design.slug for design in live] == ["atlas-coast"]
    assert missing == []


def test_brief_documents_the_whole_system(monkeypatch):
    client = _client(monkeypatch)

    async def go():
        return await client.get("northwind-bank")

    brief = render_brief(asyncio.run(go()))
    for expected in ("#F4F1EA", "28 / 20 / 16 / 13", "Send money",
                     "Balance card", "8pt grid", "Single column",
                     "https://biton-eta.vercel.app/designs/northwind-bank"):
        assert expected in brief
    assert "Build rules" in brief


def test_design_get_tool_returns_brief(monkeypatch):
    _client(monkeypatch)
    registry = ToolRegistry()
    register_design_tools(registry)

    async def go():
        ctx = ToolContext(".")
        return await registry.execute("design_get",
                                      {"design": "northwind-bank"}, ctx)

    out = asyncio.run(go())
    assert not out.is_error
    assert "#1E7A4F" in out.content
    assert out.metadata["slug"] == "northwind-bank"


def test_design_search_tool_lists_slugs(monkeypatch):
    _client(monkeypatch)
    registry = ToolRegistry()
    register_design_tools(registry)

    async def go():
        ctx = ToolContext(".")
        return await registry.execute("design_search", {"query": "travel"}, ctx)

    out = asyncio.run(go())
    assert not out.is_error
    assert "atlas-coast" in out.content
    assert "design_get" in out.content


def test_design_get_tool_reports_unknown_design(monkeypatch):
    _client(monkeypatch)
    registry = ToolRegistry()
    register_design_tools(registry)

    async def go():
        ctx = ToolContext(".")
        return await registry.execute("design_get", {"design": "nope"}, ctx)

    out = asyncio.run(go())
    assert out.is_error
    assert "nope" in out.content


def test_design_requires_reference():
    with pytest.raises(DesignError):
        asyncio.run(BitonClient().get("  "))


def test_design_model_exposes_json_summary(monkeypatch):
    client = _client(monkeypatch)

    async def go():
        return await client.get("northwind-bank")

    design: Design = asyncio.run(go())
    payload = design.to_json()
    assert payload["slug"] == "northwind-bank"
    assert payload["flow"] == ["Home", "Accounts", "Send money"]
    assert payload["palette"]["bg"] == "#F4F1EA"
    assert payload["live"] is None
