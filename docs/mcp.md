# MCP (Model Context Protocol)

Daemon ships a first-party MCP client implementing the base protocol
(JSON-RPC 2.0) over both standard transports, with the protocol revision
negotiated per server (client proposes `2025-06-18`; the server's reply
wins if compatible).

## Transports

- **stdio** — the server process is spawned by Daemon; newline-delimited
  JSON-RPC over its stdin/stdout. Environment entries support `${VAR}`
  expansion.
- **Streamable HTTP** — POST with `Accept: application/json,
  text/event-stream`; JSON **and** SSE-framed responses are handled;
  `MCP-Session-Id` is captured from responses and replayed on every
  subsequent request, alongside the `MCP-Protocol-Version` header.
- **OAuth** (HTTP servers that answer `401` with resource metadata):
  authorization-code flow with PKCE is implemented (verifier/challenge
  generation and metadata parsing are unit-tested; the live browser flow
  needs manual verification against a real server).

## Managing servers

```bash
# stdio server
daemon mcp add filesystem --command npx --arg @modelcontextprotocol/server-filesystem --arg /data

# streamable HTTP server
daemon mcp add search --url https://mcp.example.com/mcp

# with environment variables or headers
daemon mcp add db --command uvx --arg mcp-server-postgres \
    --env DATABASE_URL=${DATABASE_URL}
daemon mcp add remote --url https://host/mcp --header "Authorization=Bearer ${TOKEN}"

daemon mcp list            # state: trusted / unreviewed / disabled
daemon mcp test filesystem # connect, list tools/resources/prompts
daemon mcp enable filesystem   # after review: approves + exposes tools
daemon mcp disable filesystem
daemon mcp remove filesystem
```

## Trust model

MCP servers are **untrusted extensions until reviewed**:

- A newly added server is *enabled but unreviewed*. Daemon does not connect
  to it at agent startup and its tools do not exist in the registry.
- `daemon mcp enable <id>` asks you to confirm you reviewed the server;
  only then are its tools registered (named `mcp_<server>_<tool>`).
- Even after trust, MCP tool calls pass through the permission engine
  (default *ask*; adjustable with rules like any other tool).
- Tool descriptions from servers are marked untrusted content, exactly like
  web pages.

## In sessions

Trusted servers are connected when the agent starts; their tools appear in
`/tools` and are usable by the model like any built-in tool. `/mcp` shows
each server's state and the number of registered tools.

Supported operations: `initialize` handshake, `tools/list` (with cursor
pagination), `tools/call`, `resources/list`, `resources/read`,
`prompts/list`, `prompts/get`, `ping`, cancellation and progress
notifications (ignored gracefully), server-side notifications.

## Testing your setup

```bash
daemon mcp test <id>
```

connects, performs the handshake, and reports the server name/version,
negotiated protocol revision, tool list, resource count and prompt names.
The integration test suite drives a real stdio MCP echo server and a
streamable HTTP mock through every operation above.
