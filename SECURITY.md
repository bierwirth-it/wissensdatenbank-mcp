# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately via [GitHub security advisories](https://github.com/bierwirth-it/wissensdatenbank-mcp/security/advisories/new), not in public issues. You will get an answer within a few working days. Please include the version (image tag or commit), a description and, if possible, steps to reproduce.

## Supported versions

Security fixes are made for the latest release.

## Deployment notes

- **Never run the server open on a network you do not fully trust.** Without `MCP_AUTH_TOKEN` and without an access policy every client that reaches the port can read the whole vault; the server logs a warning in that case.
- Use HTTPS between clients and server outside private networks; tokens are sent with every request.
- Tokens are stored as SHA-256 hashes only. Treat the plain token like a password; revoke it with `wdb token revoke` when a client is retired.
- Access control works per note (see [docs/access-control.md](docs/access-control.md#what-hidden-means-for-each-tool)). Content quoted in a visible note is visible.
- The `/admin` endpoints accept only requests from `127.0.0.1` inside the container. Do not publish them through a proxy that rewrites the client address to localhost.
- Use a read-only deploy key for the vault repository. The server never writes to it.
- Assistants connected to the server pass note content to their language model. Choose the model provider accordingly.
