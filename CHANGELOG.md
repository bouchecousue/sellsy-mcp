# Changelog

## 0.5.0b4 - estimate readback

- Accept equivalent HTML line break tags (`<br>`, `<br/>`, and `<br />`) in estimate
  row descriptions during readback. Other differences still trigger a mismatch.

## 0.5.0b3 - draft estimate editing

- Change a draft estimate’s subject, append free-form or catalog rows, and remove
  existing rows by ID. Combine these edits with existing row updates in one preview.
- Review the subject and row changes before approval. Applying an approved edit
  rechecks the draft and verifies the subject, row identities, order and content.
- Update pypdf to 7.0.0 for PDF extraction, Ruff to 0.17.4 for development and
  the pinned setup-uv action to 10.3.3 for CI.
- Add English and French Sellsy setup guides.

## 0.5.0b2 - MCP SDK v2

- Migrate from `FastMCP` to the official SDK 2.3 `MCPServer` API and configure
  Streamable HTTP transport when creating the ASGI app.
- Preserve the existing OAuth issuer spelling, token storage, optional write
  scopes, stateless JSON transport and DNS-rebinding protections.
- Retain camelCase MCP annotation serialization and expose the application version.
- Suppress the SDK's new HTTP client loggers alongside the existing ones.
- Add HTTP regression coverage for dynamic registration, authenticated
  prepare/apply with untouched rows and replay refusal, revoked tokens and audience checks.
- Update pinned GitHub Actions, runtime lockfile and third-party notices.

## 0.5.0b1 - first community beta

- Standalone source distribution with an MIT license and synthetic test data.
- 42 MCP tools, per-user Sellsy OAuth and optional prepare/apply writes.
- Generic configuration, loopback defaults, Docker and Linux deployment examples.
- Read-only operation by default; writes require operator opt-in and user consent.
- OAuth resource discovery advertises optional scopes without requiring writes
  for read-only access.
- Refreshed dependencies and offline regression coverage.

The beta is derived from an internal implementation, with a new public Git
history. Private operational documents, credentials and business records are not
part of this distribution. No hosted service or PyPI release is implied.
