# Configuration and OAuth

[Guide pas à pas en français](configuration.fr.md).

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

### 1. Choose your public URL

For example, deploy this server at `https://mcp.example.com`. Use these values
consistently:

| Where | Value for this example |
|---|---|
| Server setting `SELLSY_MCP_PUBLIC_BASE_URL` | `https://mcp.example.com` |
| Redirect URL registered in Sellsy | `https://mcp.example.com/oauth/callback` |
| Remote MCP URL entered in your AI client | `https://mcp.example.com/mcp` |

The Sellsy callback belongs to **your MCP server**. The AI client's callback is
a separate URL registered through MCP OAuth; do not paste it into the Sellsy
redirect field. Forward the whole HTTPS origin as described in
[deployment](deployment.md), including discovery and OAuth routes.

### 2. Register an API v2 access in Sellsy

1. Sign in to your own Sellsy organization with permission to manage API access.
2. Open **Réglages → Portail Développeur → API v2**, or the
   [API v2 developer page](https://www.sellsy.fr/developer/api-v2).
3. Create an access, give it a recognizable name such as `MCP — My organization`,
   and select **Privé / Private**. This server signs each staff member in using
   authorization code with PKCE; a **Personnel / Personal** client using client
   credentials is a different flow.
4. Set the redirect URL to your exact `https://mcp.example.com/oauth/callback`,
   replacing the hostname with your own. Save the access with the scopes below.
5. Copy the generated **Client ID** and **Client Secret** into your private `.env`.
   Save the secret in your password manager when it is shown.

The navigation, client types and credential handling are documented in
[Sellsy's API access guide](https://help.sellsy.com/fr/articles/5876615-types-d-acces-api).
The server implements PKCE and token refresh automatically, following the
[official authentication reference](https://docs.sellsy.com/api/v2/#section/Authentication).
You do not need to generate a bearer token manually or configure Postman.

A shared integration for unrelated Sellsy accounts requires a **Public** client
and Sellsy validation. See the [official API documentation](https://docs.sellsy.com/api/v2/).
This project does not provide a shared client ID, secret or public hosted service.

### 3. Select the object scopes

For the full read toolset, select the following scopes in the Sellsy access.
They correspond to this repository's `.env.example`; a smaller selection limits
which tools can work. `staffs.read` is needed to resolve the signed-in identity.

| Tool area | Sellsy read scopes |
|---|---|
| Signed-in staff | `staffs.read` |
| Customers and contacts | `companies.read`, `contacts.read` |
| Catalog | `items.read` |
| Estimates and invoices | `estimates.read`, `invoices.read` |
| Credit notes, orders and deliveries | `credit-notes.read`, `orders.read`, `deliveries.read` |
| Opportunities | `opportunities.read` |
| Subscriptions | `subscriptions.read` |
| Comments, files and payments | `comments.read`, `files.read`, `payments.read` |
| Native text search | `search.read` |
| Taxes and linked metadata | `taxes.read`, `custom-fields.read`, `smart-tags.read` |

For optional writes, add `estimates.write` for estimates and/or
`opportunities.write` for opportunities. Match `SELLSY_MCP_SELLSY_SCOPES` to
the scope selection you want this server to request. Do not select unrelated
write scopes or API v1 access for this integration. Staff ACLs and plan
restrictions still apply even when a scope is granted.

`.env.example` contains the read scopes and the two optional write scopes.
With `SELLSY_MCP_ENABLE_WRITES=false`, new MCP authorizations request only read
scopes from Sellsy. Enabling writes also requires a client authorization requesting
`sellsy.write`, as explained below.

### 4. Configure and start your instance

```sh
cp .env.example .env
chmod 600 .env
# Edit .env before starting the service.
```

Set these entries, keeping the other settings from the example:

```dotenv
SELLSY_CLIENT_ID=YOUR-SELLSY-CLIENT-ID
SELLSY_CLIENT_SECRET=YOUR-SELLSY-CLIENT-SECRET
SELLSY_MCP_PUBLIC_BASE_URL=https://mcp.example.com
SELLSY_MCP_ENABLE_WRITES=false
```

The Client ID must be the plain UUID, without a `Client ID:` label. Paste only
the secret value into `SELLSY_CLIENT_SECRET`, without a field label or surrounding
spaces. Do not put either value into the MCP URL or the AI chat. Start the server
with `uv run --frozen --no-dev sellsy-mcp` or the Docker/systemd instructions in
[deployment](deployment.md).

### 5. Connect a user and verify access

1. In a remote MCP client supporting Streamable HTTP and OAuth, add your
   `https://mcp.example.com/mcp` URL.
2. Complete the browser sign-in and Sellsy consent using the intended staff
   account. Each collaborator signs in separately.
3. Call `sellsy_get_capabilities` from that same connected client. Confirm the
   expected identity, `upstream: reachable` and no unexpected `missing_scopes`.
4. Try a read, such as searching for a known company. A green `/health/ready`
   response alone does not validate Sellsy credentials or user permissions.

After changing scopes in Sellsy, reconnect and consent again; an existing grant
does not acquire the new permissions automatically. To enable writes, also set
`SELLSY_MCP_ENABLE_WRITES=true`, restart, request MCP scope `sellsy.write` and
check both permission layers below. A write still requires the separate
prepare, review, approval and apply workflow.

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

| Symptom | Check |
|---|---|
| Invalid client ID or `Client authentication failed` | Correct API v2 Private access, plain UUID, secret value without a copied label; restart after changing `.env` |
| Redirect mismatch or failed callback | Exact public hostname, HTTPS, `/oauth/callback`, no extra trailing slash; proxy forwards this route |
| Missing staff identity | `staffs.read` selected and granted; sign in as an active staff member |
| Missing scope / HTTP 403 | The Sellsy access's scope selection, current consent, staff ACLs and plan; changing `.env` alone is insufficient |
| Reads work but writes do not | Operator switch, MCP `sellsy.write`, Sellsy object write scope and fresh consent are all needed |
| Anonymous `/mcp` returns 401 | Expected; connect through the client's OAuth sign-in |

Check `sellsy_get_capabilities` after connection. `writes_enabled` is the global
operator switch, not proof that the current MCP token or Sellsy grant can write.
If a client continues requesting `sellsy.read` only, review its OAuth requested
scopes and cached metadata, then initiate a fresh client authorization with
`sellsy.write` and user consent. Do not weaken the server's permission checks.

Add callback hostnames only for clients you intend to trust. The default list
covers common ChatGPT callbacks and local development; other clients may need a
different exact hostname. This beta does not certify every MCP client.
