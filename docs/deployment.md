# Deployment and administration

First complete [Sellsy API access setup](configuration.md#create-the-sellsy-client)
([français](configuration.fr.md)): create your Private client, register the
callback and copy your credentials into `.env`.

Use one instance and one persistent state directory per organization. Supply
your own Sellsy client and HTTPS origin. Keep the process behind a TLS reverse
proxy or tunnel forwarding the whole origin, including `/mcp`, `/authorize`,
`/token`, `/register`, `/oauth/callback` and `/.well-known/` routes.

Run a single process: SQLite storage and read throttling are designed for one
instance, not multiple shared-volume replicas. A successful readiness check is
not proof that a user's Sellsy grant is valid.

## Docker Compose

```sh
cp .env.example .env
chmod 600 .env
# Fill credentials and your public HTTPS origin.
docker compose up --build -d
curl --fail http://127.0.0.1:8766/health/ready
docker compose exec sellsy-mcp sellsy-mcp-admin grants
```

The image runs as an unprivileged UID 10001. The example mounts a named volume
at `/app/var` and publishes only `127.0.0.1:8766`. Set up TLS separately. A proxy
inside another Docker network should use the service name and an explicit
private network rather than making the port publicly reachable.

The image's `0.0.0.0` listener is inside the container; the host binding stays
loopback. Do not mount the host Docker socket or copy `.env` into the image.
The container needs outbound HTTPS for Sellsy and authorized file downloads.

## Linux / systemd

The sample `deploy/sellsy-mcp.service` assumes a dedicated non-root `sellsy-mcp`
user and an installation at `/opt/sellsy-mcp`. Adapt paths to your host.

```sh
sudo useradd --system --home /opt/sellsy-mcp --shell /usr/sbin/nologin sellsy-mcp
sudo install -d -o sellsy-mcp -g sellsy-mcp /opt/sellsy-mcp
# Put the clean source release in /opt/sellsy-mcp; install uv for setup.
cd /opt/sellsy-mcp
uv sync --frozen --no-dev
sudo install -d -m 700 -o sellsy-mcp -g sellsy-mcp var
sudo install -m 600 -o sellsy-mcp -g sellsy-mcp .env.example .env
# Edit .env with your own credentials and origin.
sudo cp deploy/sellsy-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sellsy-mcp
```

Source and virtual environment must be readable by the service account; only
`var/` needs to be writable. The service uses loopback. Configure your own TLS
proxy; no particular commercial tunnel is required.

macOS operators can run the same `uv` entry point using their own LaunchAgent
and working directory. No private host paths or tunnel credentials are shipped.

## Checks and administration

```sh
curl --fail https://mcp.example.com/health/ready
curl --fail https://mcp.example.com/.well-known/oauth-protected-resource/mcp
uv run sellsy-mcp-admin grants
uv run sellsy-mcp-admin revoke --grant-id YOUR_GRANT_ID
```

Anonymous MCP requests should receive HTTP 401 with OAuth metadata. After user
consent, call `sellsy_get_capabilities` from the actual connected client. Revoking
a grant invalidates local sessions and removes stored upstream secrets; remove
the upstream Sellsy authorization as needed as well.

## Backups and recovery

The built-in maintenance job keeps up to seven encrypted daily database backups
under `var/backups`. They share the same host and encryption key, so operators
must also configure protected off-host backups. Keep configuration, database
backup and Fernet key together in protected recovery storage, never in Git.

```sh
uv run sellsy-mcp-admin backup --output /PROTECTED-DIRECTORY/state.fernet
uv run sellsy-mcp-admin restore --input /PROTECTED-DIRECTORY/state.fernet --output /NEW-PATH/state.sqlite3
```

The restore command refuses to overwrite an existing database and needs the
original encryption key. Stop the service for an actual recovery, restore the
matching files with restrictive permissions, and verify readiness and an
authenticated read. Loss of the key makes encrypted grants unrecoverable.

Protect and retain audit logs according to your organization's requirements;
they can contain staff identities and commercial values. The application does
not define your retention policy or guarantee a recovery time.
