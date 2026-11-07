# Design references (BITON)

Daemon can build from a documented design study instead of guessing at a
screenshot. [BITON](https://biton-eta.vercel.app) is an open, free library of
original interface studies; every study documents its palette, typography,
spacing rhythm, components and ordered screen flow — exactly the constraints
an agent needs.

## Commands

```bash
# find a reference
daemon design search "banking money transfer"
daemon design search --category "Mobile Apps" --style Minimal --live
daemon design search "dashboard" --json

# read the full spec (palette, type, spacing, components, flow)
daemon design show northwind-bank
daemon design show https://biton-eta.vercel.app/#/design/northwind-bank
daemon design show northwind-bank --json

# build from it: resolves the spec, prints the brief, starts the agent
daemon build from https://biton-eta.vercel.app/designs/northwind-bank
daemon build from northwind-bank
```

`daemon build from …` fetches the reference first (no credentials needed),
prints the brief into your scrollback, then opens the normal interactive
session with that brief as its first task. A slug works anywhere a page URL
does.

## Inside a session

Two read-only tools are part of the normal toolset:

| Tool | Purpose |
| --- | --- |
| `design_search` | keyword / platform / style / category / live-demo search |
| `design_get` | full build brief for one slug or design page URL |

The Build agent's rules tell it to call `design_get` before building from a
BITON link and to follow that design's palette, type scale, spacing,
components and **every** screen in its flow. You can also start the same flow
manually:

```
/design northwind-bank
```

## Where the spec comes from

A design page ships the whole library inside its HTML (`var DESIGNS = [...]`),
so Daemon reads the documented tokens with one plain HTTP GET — no API key,
no browser, no screenshots. The parsed catalogue is cached for six hours in
`~/.daemon/cache/biton-designs.json` (embedded images are dropped), and
`--refresh` bypasses the cache. If BITON is unreachable and a cache exists,
the cached catalogue is used.

BITON's public MCP endpoint (`https://biton-eta.vercel.app/mcp`) returns
catalogue summaries only, so Daemon reads design pages directly for the full
spec. You can still register the MCP server alongside it:

```bash
daemon mcp add biton --url https://biton-eta.vercel.app/mcp
daemon mcp enable biton
```

## Trust and permissions

Design pages are untrusted third-party content. Briefs are data: the agent
must not follow instructions found inside them, and Daemon flags content that
triggers its prompt-injection scanner. Both tools are read-only and network
egress follows the normal rules (`read_only` tools are allowed; unrelated
network tools keep asking).

Design studies are references, not assets to clone: match the documented
system, then adapt copy and data to your own product.
