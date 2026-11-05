"""Design-reference tools: the BITON interface library as agent input.

These give the Build agent the documented system behind a design study -
palette, type scale, spacing, components and flow - so it builds with
intent instead of guessing at a screenshot.
"""

from __future__ import annotations

from ..designs import PLATFORMS, STYLES, BitonClient, render_brief
from ..errors import DesignError, ToolError
from .base import Tool, ToolContext, ToolOutput


class DesignSearchTool(Tool):
    name = "design_search"
    description = (
        "Search the BITON interface library (documented UI design studies) by "
        "keyword, platform, style or category. Returns slugs, page URLs and a "
        "one-line spec per match. Use it when the user wants a reference, a "
        "look or a flow but gives no link; then call design_get for the full "
        "spec of the one you pick.")
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Keywords, e.g. 'banking money transfer'"},
            "platform": {"type": "string", "enum": PLATFORMS},
            "style": {"type": "string", "enum": STYLES},
            "category": {"type": "string",
                         "description": "e.g. Mobile Apps, Websites, "
                                        "Dashboards, Components"},
            "live": {"type": "boolean",
                     "description": "Only designs with a hosted live demo"},
            "limit": {"type": "integer",
                      "description": "Maximum results (default 8)"},
        },
        "required": [],
    }
    read_only = True
    network = True
    timeout_s = 60
    category = "design"

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        limit = int(args.get("limit") or 8)
        try:
            matches = await BitonClient().search(
                str(args.get("query") or ""),
                platform=args.get("platform"),
                style=args.get("style"),
                category=args.get("category"),
                live=args.get("live"),
                limit=limit)
        except DesignError as exc:
            raise ToolError(str(exc), hint=exc.hint) from exc
        if not matches:
            return ToolOutput(
                content="No BITON design matched those filters. Broaden the "
                        "query or drop a filter.",
                metadata={"count": 0})
        lines = [f"{len(matches)} BITON design(s):"]
        for design in matches:
            live = " [live demo]" if design.live_url else ""
            lines.append(f"- {design.slug} - {design.title} "
                         f"({design.summary_line()}){live}\n  {design.url}")
        lines.append("Read one in full with design_get before building from it.")
        return ToolOutput(content="\n".join(lines),
                          metadata={"count": len(matches),
                                    "slugs": [d.slug for d in matches]})


class DesignGetTool(Tool):
    name = "design_get"
    description = (
        "Fetch one BITON design study by slug or page URL and return its "
        "build brief: intent, ordered screen flow, exact colour palette, "
        "typography, spacing, responsive notes and components. Call this "
        "before building anything from a biton-eta.vercel.app design link, "
        "and follow the brief's palette, type scale, spacing and full flow.")
    parameters = {
        "type": "object",
        "properties": {
            "design": {"type": "string",
                       "description": "Design slug ('northwind-bank') or its "
                                      "BITON page URL"},
        },
        "required": ["design"],
    }
    read_only = True
    network = True
    timeout_s = 60
    category = "design"

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        try:
            design = await BitonClient().get(str(args["design"]))
        except DesignError as exc:
            raise ToolError(str(exc), hint=exc.hint) from exc
        return ToolOutput(content=render_brief(design),
                          metadata={"slug": design.slug, "url": design.url})


def register_design_tools(registry) -> None:
    registry.register(DesignSearchTool())
    registry.register(DesignGetTool())
