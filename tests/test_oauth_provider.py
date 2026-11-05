from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from sellsy_mcp.crypto import TokenCipher
from sellsy_mcp.oauth_provider import SellsyOAuthProvider
from sellsy_mcp.store import Store


def make_provider(settings):
    store = Store(settings.database_path)
    return SellsyOAuthProvider(settings, store, TokenCipher(settings.encryption_key_file))


def make_client(client_id="chatgpt-client"):
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="secret",
        redirect_uris=["https://chatgpt.com/oauth/callback"],
        token_endpoint_auth_method="client_secret_post",
        scope="sellsy.read sellsy.write offline_access",
    )


def jwt_with_payload(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


@pytest.mark.asyncio
async def test_oauth_callback_binds_sellsy_staff_and_issues_opaque_tokens(settings):
    provider = make_provider(settings)
    client = make_client()
    await provider.register_client(client)
    redirect = await provider.authorize(
        client,
        AuthorizationParams(
            state="chatgpt-state",
            scopes=["sellsy.read", "sellsy.write", "offline_access"],
            code_challenge="downstream-challenge",
            redirect_uri="https://chatgpt.com/oauth/callback",
            redirect_uri_provided_explicitly=True,
            resource=settings.resource_url,
        ),
    )
    query = parse_qs(urlparse(redirect).query)
    upstream_state = query["state"][0]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [settings.callback_url]

    with respx.mock(assert_all_called=True) as router:
        router.post(str(settings.sellsy_token_url)).mock(
            return_value=Response(
                200,
                json={
                    "access_token": jwt_with_payload(
                        {"sub": "authorization-uuid", "userType": "staff", "userId": 42}
                    ),
                    "refresh_token": "sellsy-refresh-alice",
                    "expires_in": 3600,
                },
            )
        )
        router.get(url__startswith=f"{str(settings.sellsy_api_base_url).rstrip('/')}/staffs").mock(
            return_value=Response(
                200,
                json={
                    "data": [
                        {"id": 42, "email": "alice@example.test", "firstname": "Alice"},
                        {"id": 99, "email": "bob@example.test", "firstname": "Bob"},
                    ]
                },
            )
        )
        downstream_redirect = await provider.complete_sellsy_callback(
            state=upstream_state, code="sellsy-code"
        )

    downstream_query = parse_qs(urlparse(downstream_redirect).query)
    code = downstream_query["code"][0]
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None
    tokens = await provider.exchange_authorization_code(client, loaded)
    assert tokens.access_token.startswith("mcp_at_")
    assert "sellsy-access" not in tokens.access_token
    access = await provider.load_access_token(tokens.access_token)
    assert access is not None
    _, identity = await provider.get_sellsy_access_token(access.grant_id)
    assert identity == {
        "grant_id": access.grant_id,
        "staff_id": "42",
        "staff_email": "alice@example.test",
        "staff_name": "Alice",
    }

    encrypted_row = provider.store.one(
        "SELECT access_token_enc, refresh_token_enc FROM sellsy_grants WHERE grant_id = ?",
        (access.grant_id,),
    )
    assert "authorization-uuid" not in encrypted_row["access_token_enc"]
    assert "sellsy-refresh-alice" not in encrypted_row["refresh_token_enc"]
    client_row = provider.store.one(
        "SELECT client_json FROM oauth_clients WHERE client_id = ?", (client.client_id,)
    )
    assert "secret" not in client_row["client_json"]
    code_row = provider.store.one("SELECT payload_json FROM oauth_codes LIMIT 1")
    assert code not in code_row["payload_json"]


@pytest.mark.asyncio
async def test_mcp_refresh_rotates_and_revoke_kills_family(settings):
    provider = make_provider(settings)
    client = make_client()
    provider.store.put_json(
        "sellsy_grants",
        "grant_id",
        "grant-1",
        {
            "staff_id": "1",
            "staff_email": None,
            "staff_name": None,
            "access_token_enc": provider.cipher.encrypt("upstream"),
            "refresh_token_enc": provider.cipher.encrypt("refresh"),
            "expires_at": int(time.time()) + 3600,
            "scope": "estimates.read",
            "revoked_at": None,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        },
    )
    issued = provider._issue_mcp_tokens(
        client_id=client.client_id,
        grant_id="grant-1",
        scopes=["sellsy.read"],
        resource=settings.resource_url,
    )
    refresh = await provider.load_refresh_token(client, issued.refresh_token)
    rotated = await provider.exchange_refresh_token(client, refresh, ["sellsy.read"])
    assert await provider.load_refresh_token(client, issued.refresh_token) is None
    rotated_access = await provider.load_access_token(rotated.access_token)
    assert rotated_access is not None
    await provider.revoke_token(rotated_access)
    assert await provider.load_access_token(rotated.access_token) is None


def test_pkce_test_vector_shape():
    verifier = "a" * 64
    challenge = hashlib.sha256(verifier.encode()).digest()
    assert len(challenge) == 32


def test_identity_subject_matches_embedded_sellsy_user_uuid():
    assert SellsyOAuthProvider._identity_matches_subject(
        {
            "id": 42,
            "_embed": {"user": {"uuid": "00000000-0000-4000-8000-000000000002"}},
        },
        {"00000000-0000-4000-8000-000000000002"},
    )


@pytest.mark.asyncio
async def test_dynamic_client_registration_rejects_untrusted_redirect(settings):
    provider = make_provider(settings)
    client = make_client("evil-client")
    client.redirect_uris = ["https://attacker.example/callback"]
    with pytest.raises(Exception, match="Redirect URI host is not allowed"):
        await provider.register_client(client)
