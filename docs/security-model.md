# Security model and known boundaries

One organization should run each instance. Individual staff sign in to Sellsy
using authorization code + PKCE. The server resolves their Sellsy identity and
uses their own grant for tool requests. No business object ID chooses a grant.

MCP tokens and Sellsy tokens are distinct. Upstream tokens and pending OAuth
state are encrypted with a local Fernet key; MCP bearer tokens are stored as
hashes. Local storage files are mode 0600, but possession of the host, key and
database can reveal upstream credentials. Staff names, emails and write audit
content are sensitive local data; the database itself is not wholly encrypted.

OAuth callbacks validate state and PKCE; MCP tokens have an audience, expiry,
scopes and rotating refresh-token family. Dynamic clients must use an approved
callback host. Keep this allowlist narrow. A change to a public origin requires
matching Sellsy callbacks and client reconnection.

## Controlled business writes

Writes are disabled by default. Operator opt-in, MCP write scope, relevant Sellsy
consent and staff ACLs are separate requirements. A preparation creates a
per-user short-lived manifest; applying it requires explicit approval and claims
it once. Existing rows are preserved and live preconditions are checked before
writing. The result is persisted and compared with a fresh readback.

The MCP client must obtain human approval. The server's `approved` parameter
cannot independently prove a human clicked an approval control. Customer
descriptions, comments and documents are untrusted content, not instructions.

Do not replay an uncertain write: use `sellsy_get_manifest_result`, read the
object and investigate. This is not a distributed transaction with Sellsy;
network failures can leave a real write with an unknown local outcome.

## Scope of this beta

- No security certification, penetration-test claim, or isolation guarantee for a
  public multi-organization hosting service.
- Single-process SQLite deployment; multiple workers/replicas need separate design.
- Local grant revocation removes local upstream secrets and MCP access. It does
  not prove the original Sellsy refresh token was revoked remotely.
- Health checks do not prove upstream OAuth or business permissions work.
- Bounded file parsing reduces exposure but is not an OS sandbox.
- Purchase costs are hidden by default in model output, but operators and the
  relevant Sellsy grants can access commercial data.
- A reverse proxy must provide TLS and appropriate ingress/body/rate limits.
  Do not put an interactive access login in front of the MCP/OAuth routes.

Use synthetic test accounts before enabling production writes. Do not infer
production validation from the mocked test suite. See SECURITY.md for private
reporting and deployment.md for encrypted backups and local revocation.
