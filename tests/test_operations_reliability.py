from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import respx
from httpx import Response
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import ValidationError

from sellsy_mcp.admin import encrypted_backup, restore_backup, revoke_grant
from sellsy_mcp.dates import in_period
from sellsy_mcp.files import approved_url
from sellsy_mcp.logging_setup import SafeLogFilter
from sellsy_mcp.models import CommentSearch, CommercialDocumentInput, EstimateSearch, EstimateUpdate
from sellsy_mcp.presentation import _margin_analysis, _summary
from sellsy_mcp.store import Store, loads, token_hash

BASE = "https://api.sellsy.com/v2"


async def test_concurrent_401_refreshes_once(runtime_tools):
    runtime, client, _ = runtime_tools
    with respx.mock as router:

        def read(request):
            return (
                Response(401, json={})
                if request.headers["authorization"] == "Bearer alice-token"
                else Response(200, json={"id": 1})
            )

        router.get(BASE + "/companies/1").mock(side_effect=read)
        token_route = router.post(str(client.settings.sellsy_token_url)).respond(
            200, json={"access_token": "rotated", "refresh_token": "rotated-refresh", "expires_in": 3600}
        )
        results = await asyncio.gather(*(client.get("/companies/1") for _ in range(4)))
    assert results == [{"id": 1}] * 4
    assert token_route.call_count == 1
    assert runtime.provider.metrics["http:401"] >= 1
    await runtime.provider.close()


async def test_429_read_retries_respecting_retry_after(runtime_tools):
    _, client, _ = runtime_tools
    with respx.mock as router:
        route = router.get(BASE + "/companies/1").mock(
            side_effect=[Response(429, headers={"Retry-After": "0"}, json={}), Response(200, json={"id": 1})]
        )
        assert await client.get("/companies/1") == {"id": 1}
    assert route.call_count == 2


async def test_composite_keeps_document_when_files_forbidden(runtime_tools):
    _, _, tools = runtime_tools
    with respx.mock as router:
        router.get(BASE + "/estimates/123").respond(200, json={"id": 123, "rows": []})
        router.get(BASE + "/estimates/123/files").respond(403, json={"error": {"code": "scope"}})
        router.get(BASE + "/estimates/123/payments").respond(
            200, json={"data": [], "pagination": {"total": 0}}
        )
        router.post(BASE + "/comments/search").respond(200, json={"data": [], "pagination": {"total": 0}})
        result = await tools["sellsy_analyze_commercial_document"](
            CommercialDocumentInput(document_type="estimate", document_id=123)
        )
    assert result["partial"]
    assert result["document"]["id"] == 123
    assert result["files"]["error"]["code"] == "permission_denied"


@pytest.mark.parametrize(
    "values",
    [
        {"date_start": "2026-02-30"},
        {"date_start": "2026-10-01", "date_end": "2026-09-30"},
        {"date_end": "2026-09-10T12:00:00"},
    ],
)
def test_invalid_dates_rejected(values):
    with pytest.raises(ValidationError):
        EstimateSearch(**values)


def test_end_date_inclusive_timezone_and_duplicate_rows():
    assert in_period("2026-09-10T21:59:59Z", None, "2026-09-10")
    assert not in_period("2026-09-10T22:00:00Z", None, "2026-09-10")
    with pytest.raises(ValidationError):
        EstimateUpdate(estimate_id=1, rows=[{"id": 1, "quantity": "2"}, {"id": 1, "quantity": "3"}])
    CommentSearch(created_end="2026-09-10")


def test_person_summary_and_margin_approximation():
    result = _summary(
        {"id": 1, "first_name": "Alice", "last_name": "Test", "email": "alice@example.test"},
        family="contacts",
    )
    assert result["email"] == "alice@example.test"
    assert result["first_name"] == "Alice"
    margin = _margin_analysis(
        {
            "rows": [
                {"id": 1, "type": "catalog", "quantity": "2", "unit_amount": "10", "purchase_amount": "5"},
                {"id": 2, "type": "subtotal", "quantity": "2", "unit_amount": "10", "purchase_amount": "5"},
            ]
        }
    )
    assert not margin["coverage"]["complete"]
    assert margin["totals"]["purchase_excl_tax"] == "10"


async def test_read_only_oauth_and_real_pkce(runtime_tools):
    runtime, _, _ = runtime_tools
    provider = runtime.provider
    client = OAuthClientInformationFull(
        client_id="test", redirect_uris=["https://chatgpt.com/oauth/callback"]
    )
    redirect = await provider.authorize(
        client,
        AuthorizationParams(
            state="test-state",
            scopes=["sellsy.read"],
            code_challenge="downstream",
            redirect_uri="https://chatgpt.com/oauth/callback",
            redirect_uri_provided_explicitly=True,
            resource=provider.settings.resource_url,
        ),
    )
    query = parse_qs(urlsplit(redirect).query)
    assert "estimates.write" not in query["scope"][0].split()
    pending = provider.store.one(
        "SELECT payload_json FROM oauth_pending WHERE state_hash=?", (token_hash(query["state"][0]),)
    )
    data = loads(provider.cipher.decrypt(pending["payload_json"]))
    verifier = data["upstream_verifier"]
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert query["code_challenge"] == [expected]


def test_backup_restore_and_local_revoke(runtime_tools, tmp_path):
    runtime, client, _ = runtime_tools
    provider = runtime.provider
    backup = tmp_path / "backup.fernet"
    restore = tmp_path / "restore.sqlite3"
    encrypted_backup(provider.store, provider.cipher, backup)
    assert b"alice" not in backup.read_bytes()
    restore_backup(provider.cipher, backup, restore)
    restored = Store(restore)
    row = restored.one("SELECT access_token_enc FROM sellsy_grants WHERE grant_id='alice'")
    assert provider.cipher.decrypt(row["access_token_enc"]) == "alice-token"
    with pytest.raises(ValueError):
        restore_backup(provider.cipher, backup, restore)
    issued = provider._issue_mcp_tokens(
        client_id="test", grant_id="alice", scopes=["sellsy.read"], resource=client.settings.resource_url
    )
    result = revoke_grant(provider.store, "alice", runtime.audit)
    assert result["status"] == "revoked_locally"
    row = provider.store.one("SELECT * FROM sellsy_grants WHERE grant_id='alice'")
    assert row["access_token_enc"] == "" and row["refresh_token_enc"] is None
    assert provider.store.one(
        "SELECT revoked_at FROM mcp_tokens WHERE token_hash=?", (token_hash(issued.access_token),)
    )["revoked_at"]


def test_expired_cursor_pruned_but_recent_outcome_retained(runtime_tools):
    runtime, client, _ = runtime_tools
    cursor = client.continuations.save("alice", {"query": "x"}, {"offset": 3})
    runtime.provider.store.execute("UPDATE search_cursors SET expires_at=?", (int(time.time()) - 1,))
    runtime.provider.store.prune()
    with pytest.raises(ValueError):
        client.continuations.load(cursor, "alice", {"query": "x"})


def test_logs_do_not_contain_oauth_or_search_query():
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        "test",
        1,
        "GET %s",
        ("https://mcp.example/oauth/callback?code=TOPSECRET&state=PRIVATE",),
        None,
    )
    assert SafeLogFilter().filter(record)
    assert "TOPSECRET" not in record.getMessage()
    assert "PRIVATE" not in record.getMessage()


@pytest.mark.parametrize(
    "url",
    [
        "http://www.sellsy.com/a",
        "https://127.0.0.1/a",
        "https://evil.example/a",
        "https://www.sellsy.com.evil.example/a",
        "https://user@www.sellsy.com/a",
    ],
)
def test_file_proxy_rejects_untrusted_urls(url):
    assert not approved_url(url, [])


@pytest.mark.parametrize("staff_id", [42, 99])
async def test_admin_reconsent_keeps_grant_and_rejects_identity_change(runtime_tools, staff_id):
    runtime, client, _ = runtime_tools
    provider = runtime.provider
    provider.store.execute(
        "UPDATE sellsy_grants SET scope='estimates.read estimates.write opportunities.read' "
        "WHERE grant_id='alice'"
    )
    url = provider.reauthorize_grant("alice", add_write_scopes=("opportunities.write",))
    query = parse_qs(urlsplit(url).query)
    payload = (
        base64.urlsafe_b64encode((f'{{"userType":"staff","userId":{staff_id}}}').encode())
        .decode()
        .rstrip("=")
    )
    token = f"header.{payload}.signature"
    with respx.mock as router:
        router.post(str(client.settings.sellsy_token_url)).respond(
            200, json={"access_token": token, "refresh_token": "new-refresh", "expires_in": 3600}
        )
        router.get(url__startswith=BASE + "/staffs").respond(
            200, json={"data": [{"id": staff_id, "firstname": "Test"}]}
        )
        if staff_id == 42:
            result = await provider.complete_sellsy_callback(state=query["state"][0], code="code")
            assert result.endswith("/oauth/complete")
            grant = provider.store.one("SELECT * FROM sellsy_grants WHERE grant_id='alice'")
            assert provider.cipher.decrypt(grant["access_token_enc"]) == token
            assert "search.read" in grant["scope"].split()
            assert "opportunities.write" in grant["scope"].split()
        else:
            with pytest.raises(PermissionError):
                await provider.complete_sellsy_callback(state=query["state"][0], code="code")
            grant = provider.store.one("SELECT * FROM sellsy_grants WHERE grant_id='alice'")
            assert provider.cipher.decrypt(grant["access_token_enc"]) == "alice-token"
            assert "opportunities.write" not in grant["scope"].split()
        with pytest.raises(ValueError):
            await provider.complete_sellsy_callback(state=query["state"][0], code="code")


def test_reconsent_write_scopes_are_explicit_configured_and_pending(runtime_tools):
    runtime, _, _ = runtime_tools
    provider = runtime.provider
    provider.store.execute(
        "UPDATE sellsy_grants SET scope='estimates.read estimates.write opportunities.read' "
        "WHERE grant_id='alice'"
    )
    default = parse_qs(urlsplit(provider.reauthorize_grant("alice")).query)["scope"][0].split()
    assert "estimates.write" in default and "opportunities.write" not in default
    explicit = parse_qs(
        urlsplit(provider.reauthorize_grant("alice", add_write_scopes=("opportunities.write",))).query
    )["scope"][0].split()
    assert "opportunities.write" in explicit
    grant = provider.store.one("SELECT scope FROM sellsy_grants WHERE grant_id='alice'")
    assert "opportunities.write" not in grant["scope"].split()
    for invalid in ("staffs.write", "opportunities.read"):
        with pytest.raises(ValueError):
            provider.reauthorize_grant("alice", add_write_scopes=(invalid,))
