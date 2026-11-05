# Configuration and OAuth

Copy `.env.example` to `.env`. Keep the real file private (mode 0600). Settings
are loaded from `.env` and environment variables. Generic defaults use loopback
and relative `var/` paths; remote use requires an explicit HTTPS public origin.

| Setting | Purpose |
|---|---|
| `SELLSY_CLIENT_ID`, `SELLSY_CLIENT_SECRET` | Your own Sellsy OAuth application's credentials |
| `SELLSY_MCP_PUBLIC_BASE_URL` | Public origin, for example `https://mcp.example.com`; no `/mcp` suffix |
| `SELLSY_MCP_HOST`, `SELLSY_MCP_PORT` | Listener, default `127.0.0.1:8766` |
| `SELLSY_MCP_DATABASE_PATH` | SQLite state, default `var/sellsy-mcp.sqlite3` |
| `SELLSY_MCP_ENCRYPTION_KEY_FILE` | Fernet key, default `var/fernet.key` |
| `SELLSY_MCP_AUDIT_LOG_PATH` | Private write audit, default `var/audit.jsonl` |
| `SELLSY_MCP_ALLOWED_CLIENT_REDIRECT_HOSTS` | Space-separated exact client callback hostnames |
| `SELLSY_MCP_ENABLE_WRITES` | Operator opt-in, default `false` |
| `SELLSY_MCP_SELLSY_SCOPES` | Available Sellsy scopes; write scopes are filtered out for read-only consent |
| `SELLSY_MCP_FILE_DOWNLOAD_HOSTS` | JSON list of additional exact HTTPS file hosts; empty by default |

Defaults also bound search pages, timeouts, request concurrency and pending
reads. No business-write retries are enabled. File reads are bounded, permit
Sellsy HTTPS hosts by default, validate each redirect, and use a timed PDF worker.

## Create the Sellsy client

Use a **Private** client for staff in your own Sellsy organization, authorization
code flow with PKCE and refresh tokens. Register the exact callback:
`PUBLIC_BASE_URL/oauth/callback`. The example scopes describe the implemented
tools. `staffs.read` is needed to resolve the signed-in staff identity.
Scopes do not replace the staff member's Sellsy ACLs or plan restrictions.

A shared integration for unrelated Sellsy accounts requires a **Public** client
and Sellsy validation. See the [official API documentation](https://docs.sellsy.com/api/v2/).
This project does not provide a shared client ID, secret or public hosted service.

## Two independent permission layers

- MCP: `sellsy.read`, optional `sellsy.write`, and `offline_access` for refresh.
- Sellsy: object scopes such as `estimates.read` / `estimates.write`.

Changing configuration does not expand existing grants. Reconnect and obtain
fresh consent. Resource discovery advertises optional writes only when enabled;
the HTTP minimum remains `sellsy.read`, and write tools check `sellsy.write`.

The local admin can prepare same-staff Sellsy reconsent:

```sh
uv run sellsy-mcp-admin grants
uv run sellsy-mcp-admin reauthorize --grant-id YOUR_GRANT_ID --add-write-scope estimates.write
```

The generated URL is short-lived; the user completes consent. This updates
Sellsy permission only, not the MCP client's session scopes. Never edit token
tables to repair a connection.

## Client troubleshooting

Check `sellsy_get_capabilities` after connection. `writes_enabled` is the global
operator switch, not proof that the current MCP token or Sellsy grant can write.
If a client continues requesting `sellsy.read` only, review its OAuth requested
scopes and cached metadata, then initiate a fresh client authorization with
`sellsy.write` and user consent. Do not weaken the server's permission checks.

Add callback hostnames only for clients you intend to trust. The default list
covers common ChatGPT callbacks and local development; other clients may need a
different exact hostname. This beta does not certify every MCP client.
