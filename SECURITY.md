# Security policy

This is a beta self-hosted integration that can access sensitive CRM and
commercial information. Only the latest published beta is maintained; there is
no guaranteed response time or independently certified security assessment.

## Reporting a vulnerability

Use this repository's [private vulnerability reporting form](https://github.com/bouchecousue/sellsy-mcp/security/advisories/new).
Do not open a public issue containing credentials, customer data, exploit details
or a production URL. If private reporting is unavailable, open an issue asking
for a private contact channel without describing the vulnerability.

Include affected version, a minimal synthetic reproduction and expected impact.
Do not send real access/refresh tokens, database snapshots or encryption keys.

## Operator responsibilities

Use your own Sellsy OAuth client and HTTPS endpoint, restrict client redirect
hosts, protect the host and encrypted state, and keep dependencies current.
Start read-only and explicitly approve writes. Back up the encryption key and
database together in protected storage. See [security model](docs/security-model.md)
and [deployment](docs/deployment.md) for known boundaries and recovery procedures.
