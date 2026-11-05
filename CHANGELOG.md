# Changelog

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
