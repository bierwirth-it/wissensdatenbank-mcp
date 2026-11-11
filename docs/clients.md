# Clients

The endpoint is `https://<host>/mcp` (transport *Streamable HTTP*). Every client sends its account's token as `Authorization: Bearer <token>`.

Tell the assistant how to use the knowledge base, for example in its system prompt:

> Answer questions about our processes from the knowledge base only. Search with `search` first, read the full note with `read_note` when needed, follow `links`/`backlinks` if the first note does not fully answer the question. If nothing relevant is found, say so instead of guessing. Cite the file path of every source.

`MCP_INSTRUCTIONS` sets the server's own instructions, which many clients show to the model automatically – describe the vault there (language, structure, what it contains).

## Claude Code

```bash
claude mcp add --transport http knowledge-base https://kb-mcp.example.com/mcp \
  --header "Authorization: Bearer wdb_…"
```

## Claude Desktop and other stdio-only clients

Use the [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) bridge in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "knowledge-base": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://kb-mcp.example.com/mcp",
               "--header", "Authorization: Bearer ${WDB_TOKEN}"],
      "env": {"WDB_TOKEN": "wdb_…"}
    }
  }
}
```

## Hermes Agent

In the profile's `config.yaml`:

```yaml
mcp_servers:
  knowledge-base:
    url: https://kb-mcp.example.com/mcp
    headers:
      Authorization: Bearer ${WDB_TOKEN}
    timeout: 120
```

and `WDB_TOKEN=wdb_…` in the profile's `.env`. A dedicated profile per account gives each bot exactly the rights of its token.

## Open WebUI

Recent versions of Open WebUI can connect MCP servers over Streamable HTTP as tool servers (admin settings → *External Tools*): type *MCP (Streamable HTTP)*, URL `https://kb-mcp.example.com/mcp`, auth *Bearer* with the token. To give user groups different rights, add the server once per account (each with its own token) and restrict each tool server to the matching group, if your version offers access control for tool servers.

## Any HTTP client

```bash
TOKEN=wdb_…
curl -s https://kb-mcp.example.com/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search","arguments":{"query":"How do I request vacation?","top_k":3}}}'
```

The server runs in stateless mode: no session handshake is required, every request stands alone.
