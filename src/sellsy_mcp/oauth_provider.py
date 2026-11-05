from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from collections import Counter
from typing import Any
from urllib.parse import urlencode, urlparse
from uuid import uuid4

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import Settings
from .crypto import TokenCipher
from .errors import SellsyOAuthError
from .store import Store, dumps, loads, token_hash

LOGGER = logging.getLogger(__name__)


class SellsyAuthorizationCode(AuthorizationCode):
    grant_id: str


class SellsyRefreshToken(RefreshToken):
    grant_id: str
    family_id: str


class SellsyAccessToken(AccessToken):
    grant_id: str


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(72)[:128]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _opaque_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(36)}"


class SellsyOAuthProvider(
    OAuthAuthorizationServerProvider[SellsyAuthorizationCode, SellsyRefreshToken, SellsyAccessToken]
):
    """OAuth authorization-server facade backed by one isolated Sellsy grant per member."""

    def __init__(self, settings: Settings, store: Store, cipher: TokenCipher):
        self.settings = settings
        self.metrics = Counter()
        self.store = store
        self.cipher = cipher
        self._grant_locks: dict[str, asyncio.Lock] = {}
        self._http_client: httpx.AsyncClient | None = None
        self.request_semaphore = asyncio.Semaphore(settings.request_concurrency)

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=self.settings.request_timeout_seconds)
        return self._http_client

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()

    async def refresh_after_401(self, grant_id: str, failed_token: str) -> tuple[str, dict[str, Any]]:
        lock = self._grant_locks.setdefault(grant_id, asyncio.Lock())
        async with lock:
            grant = self.store.one("SELECT * FROM sellsy_grants WHERE grant_id=?", (grant_id,))
            if not grant or grant["revoked_at"] is not None:
                raise PermissionError("Sellsy authorization is missing or revoked")
            if self.cipher.decrypt(grant["access_token_enc"]) == failed_token:
                await self._refresh_sellsy_grant(grant)
        return await self.get_sellsy_access_token(grant_id)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self.store.one("SELECT client_json FROM oauth_clients WHERE client_id = ?", (client_id,))
        return (
            OAuthClientInformationFull.model_validate(loads(self.cipher.decrypt(row["client_json"])))
            if row
            else None
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("Dynamic client registration did not provide client_id")
        for redirect_uri in client_info.redirect_uris or []:
            parsed = urlparse(str(redirect_uri))
            host = (parsed.hostname or "").casefold().rstrip(".")
            if parsed.scheme not in {"https", "http"} or not self._redirect_host_allowed(host):
                from mcp.server.auth.provider import RegistrationError

                raise RegistrationError(
                    "invalid_redirect_uri",
                    "Redirect URI host is not allowed for this private MCP server",
                )
            if parsed.scheme != "https" and host not in {"localhost", "127.0.0.1"}:
                from mcp.server.auth.provider import RegistrationError

                raise RegistrationError("invalid_redirect_uri", "Redirect URI must use HTTPS")
        self.store.put_json(
            "oauth_clients",
            "client_id",
            client_info.client_id,
            {
                "client_json": self.cipher.encrypt(client_info.model_dump_json()),
                "created_at": int(time.time()),
            },
        )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not client.client_id:
            raise AuthorizeError("invalid_request", "Missing client_id")
        if params.resource and params.resource != self.settings.resource_url:
            raise AuthorizeError("invalid_target", "Unsupported resource audience")
        defaults = ["sellsy.read", "offline_access"] + (
            ["sellsy.write"] if self.settings.enable_writes else []
        )
        requested_scopes = params.scopes or defaults
        if "sellsy.write" in requested_scopes and not self.settings.enable_writes:
            raise AuthorizeError("invalid_scope", "Writes are disabled")
        if not set(requested_scopes).issubset({"sellsy.read", "sellsy.write", "offline_access"}):
            raise AuthorizeError("invalid_scope", "Unsupported MCP scope")

        upstream_state = secrets.token_urlsafe(32)
        upstream_verifier, upstream_challenge = _pkce_pair()
        expires_at = int(time.time()) + 600
        payload = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "downstream_state": params.state,
            "downstream_code_challenge": params.code_challenge,
            "downstream_scopes": requested_scopes,
            "resource": params.resource or self.settings.resource_url,
            "upstream_verifier": upstream_verifier,
            "upstream_scopes": [
                s
                for s in self.settings.scope_list
                if "sellsy.write" in requested_scopes or not s.endswith(".write")
            ],
        }
        self.store.put_json(
            "oauth_pending",
            "state_hash",
            token_hash(upstream_state),
            {"payload_json": self.cipher.encrypt(dumps(payload)), "expires_at": expires_at},
        )

        query = {
            "response_type": "code",
            "client_id": self.settings.sellsy_client_id,
            "redirect_uri": self.settings.callback_url,
            "code_challenge": upstream_challenge,
            "code_challenge_method": "S256",
            "state": upstream_state,
            "scope": " ".join(
                scope
                for scope in self.settings.scope_list
                if "sellsy.write" in requested_scopes or not scope.endswith(".write")
            ),
        }
        return f"{str(self.settings.sellsy_authorize_url)}?{urlencode(query)}"

    def reauthorize_grant(self, grant_id: str, *, add_write_scopes: tuple[str, ...] = ()) -> str:
        """Local admin starts a consent renewal, strictly bound to the existing staff identity."""
        grant = self.store.one("SELECT * FROM sellsy_grants WHERE grant_id=?", (grant_id,))
        if not grant or grant["revoked_at"] is not None:
            raise PermissionError("Grant unavailable")
        existing = set((grant["scope"] or "").split())
        configured_writes = {scope for scope in self.settings.scope_list if scope.endswith(".write")}
        requested_writes = set(add_write_scopes)
        if not requested_writes <= configured_writes:
            raise ValueError("Only configured write scopes can be requested for consent")
        # Default retains writes. An explicit admin request can ask for additional consent,
        # without changing the grant until Sellsy completes the same-staff OAuth callback.
        scopes = [
            scope
            for scope in self.settings.scope_list
            if not scope.endswith(".write") or scope in existing or scope in requested_writes
        ]
        state = secrets.token_urlsafe(32)
        verifier, challenge = _pkce_pair()
        pending = {
            "reauthorize_grant_id": grant_id,
            "staff_id": grant["staff_id"],
            "upstream_verifier": verifier,
            "upstream_scopes": scopes,
            "redirect_uri": self.settings.public_base + "/oauth/complete",
        }
        self.store.put_json(
            "oauth_pending",
            "state_hash",
            token_hash(state),
            {"payload_json": self.cipher.encrypt(dumps(pending)), "expires_at": int(time.time()) + 600},
        )
        return (
            str(self.settings.sellsy_authorize_url)
            + "?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": self.settings.sellsy_client_id,
                    "redirect_uri": self.settings.callback_url,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": state,
                    "scope": " ".join(scopes),
                }
            )
        )

    async def complete_sellsy_callback(
        self, *, state: str, code: str | None, error: str | None = None
    ) -> str:
        row = self.store.one(
            "SELECT payload_json, expires_at FROM oauth_pending WHERE state_hash = ?",
            (token_hash(state),),
        )
        if not row or row["expires_at"] < int(time.time()):
            raise ValueError("OAuth state is invalid or expired")
        if self.store.execute("DELETE FROM oauth_pending WHERE state_hash = ?", (token_hash(state),)) != 1:
            raise ValueError("OAuth state already consumed")
        pending = loads(self.cipher.decrypt(row["payload_json"]))
        if error:
            return construct_redirect_uri(
                pending["redirect_uri"],
                error="access_denied",
                error_description="Sellsy authorization was denied",
                state=pending.get("downstream_state"),
            )
        if not code:
            raise ValueError("Sellsy callback did not contain an authorization code")

        token_payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.callback_url,
            "client_id": self.settings.sellsy_client_id,
            "client_secret": self.settings.sellsy_client_secret,
            "code_verifier": pending["upstream_verifier"],
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(str(self.settings.sellsy_token_url), json=token_payload)
            self._raise_upstream_auth_error(response)
            sellsy_tokens = response.json()
            access_token = sellsy_tokens.get("access_token")
            if not access_token:
                raise RuntimeError("Sellsy token response omitted access_token")
            identity = await self._resolve_sellsy_identity(client, access_token, sellsy_tokens)

        staff_id, staff_email, staff_name = self._parse_identity(identity)
        if pending.get("reauthorize_grant_id"):
            if staff_id != pending["staff_id"]:
                raise PermissionError("Consent identity differs from the original grant")
            if not sellsy_tokens.get("refresh_token"):
                raise ValueError("Reauthorization requires a refresh token")
            now = int(time.time())
            grant_id = pending["reauthorize_grant_id"]
            lock = self._grant_locks.setdefault(grant_id, asyncio.Lock())
            async with lock:
                updated = self.store.execute(
                    "UPDATE sellsy_grants SET access_token_enc=?,refresh_token_enc=?,expires_at=?,scope=?,"
                    "updated_at=? WHERE grant_id=? AND staff_id=? AND revoked_at IS NULL",
                    (
                        self.cipher.encrypt(access_token),
                        self.cipher.encrypt(sellsy_tokens["refresh_token"]),
                        now + int(sellsy_tokens.get("expires_in", 86400)),
                        sellsy_tokens.get("scope") or " ".join(pending["upstream_scopes"]),
                        now,
                        grant_id,
                        staff_id,
                    ),
                )
            if updated != 1:
                raise PermissionError("Grant revoked during consent")
            from .audit import AuditLog

            AuditLog(self.settings.audit_log_path).write(
                {"tool": "admin.reauthorize_grant", "staff_id": staff_id, "result": "reauthorized"}
            )
            return self.settings.public_base + "/oauth/complete"
        grant_id = str(uuid4())
        now = int(time.time())
        self.store.put_json(
            "sellsy_grants",
            "grant_id",
            grant_id,
            {
                "staff_id": staff_id,
                "staff_email": staff_email,
                "staff_name": staff_name,
                "access_token_enc": self.cipher.encrypt(access_token),
                "refresh_token_enc": (
                    self.cipher.encrypt(sellsy_tokens["refresh_token"])
                    if sellsy_tokens.get("refresh_token")
                    else None
                ),
                "expires_at": now + int(sellsy_tokens.get("expires_in", 86400)),
                "scope": sellsy_tokens.get("scope") or " ".join(pending["upstream_scopes"]),
                "revoked_at": None,
                "created_at": now,
                "updated_at": now,
            },
        )

        downstream_code = _opaque_token("mcp_code")
        code_model = SellsyAuthorizationCode(
            code=downstream_code,
            scopes=pending["downstream_scopes"],
            expires_at=time.time() + 300,
            client_id=pending["client_id"],
            code_challenge=pending["downstream_code_challenge"],
            redirect_uri=pending["redirect_uri"],
            redirect_uri_provided_explicitly=pending["redirect_uri_provided_explicitly"],
            resource=pending["resource"],
            grant_id=grant_id,
        )
        self.store.put_json(
            "oauth_codes",
            "token_hash",
            token_hash(downstream_code),
            {
                "payload_json": dumps(code_model.model_dump(mode="json", exclude={"code"})),
                "expires_at": int(code_model.expires_at),
                "used_at": None,
            },
        )
        return construct_redirect_uri(
            pending["redirect_uri"], code=downstream_code, state=pending.get("downstream_state")
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> SellsyAuthorizationCode | None:
        row = self.store.one(
            "SELECT payload_json, expires_at, used_at FROM oauth_codes WHERE token_hash = ?",
            (token_hash(authorization_code),),
        )
        if not row or row["used_at"] is not None or row["expires_at"] < int(time.time()):
            return None
        payload = loads(row["payload_json"])
        payload["code"] = authorization_code
        model = SellsyAuthorizationCode.model_validate(payload)
        return model if model.client_id == client.client_id else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: SellsyAuthorizationCode
    ) -> OAuthToken:
        used = self.store.execute(
            "UPDATE oauth_codes SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
            (int(time.time()), token_hash(authorization_code.code)),
        )
        if used != 1:
            raise TokenError("invalid_grant", "Authorization code was already used")
        return self._issue_mcp_tokens(
            client_id=str(client.client_id),
            grant_id=authorization_code.grant_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> SellsyRefreshToken | None:
        row = self.store.one(
            "SELECT * FROM mcp_tokens WHERE token_hash = ? AND kind = 'refresh'",
            (token_hash(refresh_token),),
        )
        if not self._valid_token_row(row, str(client.client_id)):
            return None
        return SellsyRefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=loads(row["scopes_json"]),
            expires_at=row["expires_at"],
            grant_id=row["grant_id"],
            family_id=row["family_id"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: SellsyRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if not set(scopes).issubset(refresh_token.scopes):
            raise TokenError("invalid_scope", "Refresh cannot expand scopes")
        grant = self.store.one(
            "SELECT revoked_at FROM sellsy_grants WHERE grant_id=?", (refresh_token.grant_id,)
        )
        if not grant or grant["revoked_at"] is not None:
            raise TokenError("invalid_grant", "Grant revoked")
        revoked = self.store.execute(
            "UPDATE mcp_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (int(time.time()), token_hash(refresh_token.token)),
        )
        if revoked != 1:
            raise TokenError("invalid_grant", "Refresh token was already used")
        return self._issue_mcp_tokens(
            client_id=str(client.client_id),
            grant_id=refresh_token.grant_id,
            scopes=scopes,
            resource=self.settings.resource_url,
            family_id=refresh_token.family_id,
        )

    async def load_access_token(self, token: str) -> SellsyAccessToken | None:
        row = self.store.one(
            "SELECT * FROM mcp_tokens WHERE token_hash = ? AND kind = 'access'",
            (token_hash(token),),
        )
        if not self._valid_token_row(row) or row["resource"] != self.settings.resource_url:
            return None
        grant = self.store.one("SELECT revoked_at FROM sellsy_grants WHERE grant_id = ?", (row["grant_id"],))
        if not grant or grant["revoked_at"] is not None:
            return None
        return SellsyAccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=loads(row["scopes_json"]),
            expires_at=row["expires_at"],
            resource=row["resource"],
            grant_id=row["grant_id"],
        )

    async def revoke_token(self, token: SellsyAccessToken | SellsyRefreshToken) -> None:
        now = int(time.time())
        row = self.store.one(
            "SELECT family_id FROM mcp_tokens WHERE token_hash = ?", (token_hash(token.token),)
        )
        if row:
            self.store.execute(
                "UPDATE mcp_tokens SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL",
                (now, row["family_id"]),
            )

    async def get_sellsy_access_token(self, grant_id: str) -> tuple[str, dict[str, Any]]:
        lock = self._grant_locks.setdefault(grant_id, asyncio.Lock())
        async with lock:
            grant = self.store.one("SELECT * FROM sellsy_grants WHERE grant_id = ?", (grant_id,))
            if not grant or grant["revoked_at"] is not None:
                raise PermissionError("Sellsy authorization is missing or revoked")
            if grant["expires_at"] <= int(time.time()) + 60:
                grant = await self._refresh_sellsy_grant(grant)
            identity = {
                "grant_id": grant_id,
                "staff_id": grant["staff_id"],
                "staff_email": grant["staff_email"],
                "staff_name": grant["staff_name"],
            }
            return self.cipher.decrypt(grant["access_token_enc"]), identity

    async def _refresh_sellsy_grant(self, grant: dict[str, Any]) -> dict[str, Any]:
        if not grant.get("refresh_token_enc"):
            raise PermissionError("Sellsy session expired; reconnect the app")
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.cipher.decrypt(grant["refresh_token_enc"]),
            "client_id": self.settings.sellsy_client_id,
            "client_secret": self.settings.sellsy_client_secret,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(str(self.settings.sellsy_token_url), json=payload)
        self._raise_upstream_auth_error(response)
        tokens = response.json()
        now = int(time.time())
        values = (
            self.cipher.encrypt(tokens["access_token"]),
            self.cipher.encrypt(
                tokens.get("refresh_token") or self.cipher.decrypt(grant["refresh_token_enc"])
            ),
            now + int(tokens.get("expires_in", 86400)),
            tokens.get("scope") or grant.get("scope"),
            now,
            grant["grant_id"],
        )
        self.store.execute(
            "UPDATE sellsy_grants SET access_token_enc=?, refresh_token_enc=?, expires_at=?, scope=?, "
            "updated_at=? WHERE grant_id=?",
            values,
        )
        refreshed = self.store.one("SELECT * FROM sellsy_grants WHERE grant_id = ?", (grant["grant_id"],))
        if not refreshed:
            raise RuntimeError("Refreshed Sellsy grant disappeared")
        return refreshed

    def _issue_mcp_tokens(
        self,
        *,
        client_id: str,
        grant_id: str,
        scopes: list[str],
        resource: str | None,
        family_id: str | None = None,
    ) -> OAuthToken:
        now = int(time.time())
        family_id = family_id or str(uuid4())
        access = _opaque_token("mcp_at")
        refresh = _opaque_token("mcp_rt")
        common = {
            "client_id": client_id,
            "grant_id": grant_id,
            "scopes_json": dumps(scopes),
            "resource": resource or self.settings.resource_url,
            "revoked_at": None,
            "family_id": family_id,
            "created_at": now,
        }
        self.store.put_json(
            "mcp_tokens",
            "token_hash",
            token_hash(access),
            {**common, "kind": "access", "expires_at": now + self.settings.mcp_access_token_ttl_seconds},
        )
        self.store.put_json(
            "mcp_tokens",
            "token_hash",
            token_hash(refresh),
            {
                **common,
                "kind": "refresh",
                "expires_at": now + self.settings.mcp_refresh_token_ttl_seconds,
            },
        )
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            expires_in=self.settings.mcp_access_token_ttl_seconds,
            scope=" ".join(scopes),
        )

    @staticmethod
    def _valid_token_row(row: dict[str, Any] | None, client_id: str | None = None) -> bool:
        return bool(
            row
            and row["revoked_at"] is None
            and row["expires_at"] > int(time.time())
            and (client_id is None or row["client_id"] == client_id)
        )

    def _redirect_host_allowed(self, host: str) -> bool:
        return any(
            host == allowed or host.endswith(f".{allowed}") for allowed in self.settings.redirect_host_list
        )

    async def _resolve_sellsy_identity(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        token_response: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve the authorized Sellsy staff without relying on a non-existent /staffs/me route."""
        subjects = self._token_subjects(access_token, token_response)
        headers = {"Authorization": f"Bearer {access_token}"}
        base_url = str(self.settings.sellsy_api_base_url).rstrip("/")
        candidates: list[dict[str, Any]] = []

        for page in range(self.settings.search_max_pages):
            response = await client.get(
                f"{base_url}/staffs",
                headers=headers,
                params=[
                    ("limit", str(self.settings.search_page_size)),
                    ("offset", str(page * self.settings.search_page_size)),
                ],
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data", payload)
            page_candidates = data if isinstance(data, list) else [data]
            page_candidates = [item for item in page_candidates if isinstance(item, dict)]
            candidates.extend(page_candidates)

            if subjects:
                matches = [item for item in candidates if self._identity_matches_subject(item, subjects)]
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    raise RuntimeError("Sellsy OAuth subject matched more than one staff")

            if len(page_candidates) < self.settings.search_page_size:
                break

        if subjects:
            for candidate in candidates:
                staff_id = candidate.get("id")
                if staff_id is None:
                    continue
                response = await client.get(
                    f"{base_url}/staffs/{staff_id}",
                    headers=headers,
                    params=[("embed[]", "user")],
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                detailed_identity = response.json()
                if self._identity_matches_subject(detailed_identity, subjects):
                    return detailed_identity

        if len(candidates) == 1:
            return candidates[0]
        LOGGER.info(
            "Sellsy identity resolution did not match any of %d visible staff records",
            len(candidates),
        )
        raise RuntimeError("Unable to bind the Sellsy OAuth grant to one staff member")

    @staticmethod
    def _token_subjects(access_token: str, token_response: dict[str, Any]) -> set[str]:
        subjects: set[str] = set()
        for key in ("sub", "staff_id", "staffId", "user_id", "userId"):
            value = token_response.get(key)
            if value is not None:
                subjects.add(str(value))

        parts = access_token.split(".")
        if len(parts) == 3:
            try:
                payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode())
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            for key in ("sub", "staff_id", "staffId", "user_id", "userId"):
                value = payload.get(key)
                if value is not None:
                    subjects.add(str(value))
        return subjects

    @staticmethod
    def _identity_matches_subject(identity: dict[str, Any], subjects: set[str]) -> bool:
        identity = identity.get("data") if isinstance(identity.get("data"), dict) else identity
        embedded_user = (
            identity.get("_embed", {}).get("user", {}) if isinstance(identity.get("_embed"), dict) else {}
        )
        identifiers = {
            str(value)
            for value in (
                identity.get("id"),
                identity.get("staff_id"),
                identity.get("user_id"),
                embedded_user.get("id"),
                embedded_user.get("uuid"),
            )
            if value is not None
        }
        return bool(identifiers & subjects)

    @staticmethod
    def _parse_identity(identity: dict[str, Any]) -> tuple[str, str | None, str | None]:
        data = identity.get("data") if isinstance(identity.get("data"), dict) else identity
        embedded_user = data.get("_embed", {}).get("user", {}) if isinstance(data.get("_embed"), dict) else {}
        staff_id = data.get("id") or data.get("staff_id") or embedded_user.get("id")
        email = data.get("email") or embedded_user.get("email")
        name = data.get("name")
        if not name:
            name = (
                " ".join(
                    str(part)
                    for part in (
                        data.get("first_name") or data.get("firstname"),
                        data.get("last_name") or data.get("lastname"),
                    )
                    if part
                )
                or None
            )
        if not staff_id:
            raise RuntimeError("Sellsy staff response omitted a stable staff identifier")
        return str(staff_id), str(email) if email else None, name

    @staticmethod
    def _raise_upstream_auth_error(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            error = response.json()
            error = error if isinstance(error, dict) else {}
        except ValueError:
            error = {}
        # Never propagate upstream text: it can echo secrets or callback parameters.
        reason = error.get("error")
        if reason not in ("invalid_client", "invalid_grant", "invalid_scope", "access_denied"):
            message = error.get("error_description") or error.get("message")
            reason = "invalid_client" if message == "Client authentication failed" else "authorization_error"
        raise SellsyOAuthError(response.status_code, reason)
