# MCP Python SDK v2 migration

Version 0.5.0b2 requires `mcp[cli]>=2.3,<3`. Simply widening the old dependency
bound is insufficient: SDK v2 removes `mcp.server.fastmcp.FastMCP` and changes
where HTTP transport options are configured.

The server now uses `mcp.server.mcpserver.MCPServer`. Streamable HTTP options,
including stateless JSON responses, listener host and DNS-rebinding allowlists,
are passed to `streamable_http_app()`. Uvicorn still controls the real binding.
The existing application lifespan continues running maintenance and shutting
down the provider's HTTP pool.

## Existing installations

Back up the database and its matching encryption key, then update the source and
run `uv sync --frozen --no-dev` before restarting the single process. Retain your
existing `.env`, state directory, public origin and registered callback URL.
No database schema migration or automatic permission expansion is introduced.

SDK v2 stops automatically appending a slash to pathless OAuth issuers. This
application deliberately keeps the prior normalized issuer URL and uses the same
value in authorization-server and protected-resource metadata. MCP resource
audiences and callback paths are unchanged. Check `sellsy_get_capabilities` from
the actual connected client after the upgrade; readiness alone is insufficient.

The business API client deliberately remains on `httpx` and its existing mocked
tests. SDK v2 uses `httpx2` internally; both library families' request loggers are
disabled to avoid exposing OAuth query strings. SDK v2 adds OpenTelemetry API
support. This application does not configure a telemetry exporter; integrators
with their own global telemetry setup should review the SDK's tracing behavior.

## Validation

The suite retains the existing OAuth, isolation, scope, manifest, readback and
pagination tests. Additional HTTP checks exercise dynamic client registration,
an authenticated prepare/apply cycle without losing untouched rows, replay
refusal, and HTTP 401 for revoked or wrong-audience tokens. The legacy 2025
initialize handshake and JSON field spellings remain covered.

The container must also build and start with synthetic credentials, expose the
same 42 tools, reject anonymous MCP requests and advertise read-only scopes by
default. These tests do not certify all MCP clients or replace a live operator
check after deployment.

References: [official migration guide](https://py.sdk.modelcontextprotocol.io/v2/migration/)
and [SDK source](https://github.com/modelcontextprotocol/python-sdk).
