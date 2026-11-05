from __future__ import annotations

import copy
import json

import pytest
import respx
from httpx import Response
from starlette.testclient import TestClient

from sellsy_mcp.server import create_app, create_server

BASE = "https://api.sellsy.com/v2"


def test_authenticated_http_write_preserves_rows_and_rejects_replay(runtime_tools, settings):
    seeded, _, _ = runtime_tools
    token = seeded.provider._issue_mcp_tokens(
        client_id="synthetic-write-client",
        grant_id="alice",
        scopes=["sellsy.read", "sellsy.write"],
        resource=settings.resource_url,
    )
    mcp, runtime = create_server(settings)
    before = {
        "id": 123,
        "status": "draft",
        "subject": "Synthetic estimate",
        "rows": [
            {"id": 1, "type": "single", "description": "Item", "quantity": "1", "unit_amount": "99"},
            {"id": 2, "type": "single", "description": "Service", "quantity": "2", "unit_amount": "100"},
        ],
    }
    after = copy.deepcopy(before)
    after["rows"][0]["quantity"] = "3"
    headers = {
        "host": "127.0.0.1:8766",
        "Authorization": f"Bearer {token.access_token}",
        "Accept": "application/json, text/event-stream",
    }
    with respx.mock as router, TestClient(create_app(mcp, runtime)) as client:
        get = router.get(BASE + "/estimates/123").respond(200, json=before)
        put = router.put(BASE + "/estimates/123").respond(200, json=after)

        def call(name, params):
            response = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": {"params": params}},
                },
            )
            assert response.status_code == 200
            return response.json()["result"]["structuredContent"]

        initialized = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic-client", "version": "1"},
                },
            },
        )
        assert initialized.json()["result"]["serverInfo"]["version"] == settings.version
        prepared = call(
            "sellsy_prepare_estimate_update",
            {"estimate_id": 123, "rows": [{"id": 1, "quantity": "3"}]},
        )
        assert put.call_count == 0
        get.side_effect = [Response(200, json=before), Response(200, json=after)]
        params = {"manifest_id": prepared["manifest_id"], "approved": True}
        applied = call("sellsy_apply_estimate_update", params)
        assert applied["applied"] and applied["readback_verified"]
        assert json.loads(put.calls[0].request.content)["rows"][1] == {"id": 2}
        replay = call("sellsy_apply_estimate_update", params)
        assert replay["write_attempted"] is False
        assert put.call_count == 1


@pytest.mark.parametrize("invalid", ["other_resource", "revoked"])
def test_http_rejects_wrong_audience_and_revoked_tokens(runtime_tools, settings, invalid):
    seeded, _, _ = runtime_tools
    token = seeded.provider._issue_mcp_tokens(
        client_id="synthetic-read-client",
        grant_id="alice",
        scopes=["sellsy.read"],
        resource="https://other.example/mcp" if invalid == "other_resource" else settings.resource_url,
    )
    if invalid == "revoked":
        seeded.provider.store.execute("UPDATE mcp_tokens SET revoked_at=1 WHERE grant_id='alice'")
    mcp, runtime = create_server(settings)
    with TestClient(create_app(mcp, runtime)) as client:
        response = client.post(
            "/mcp",
            headers={
                "host": "127.0.0.1:8766",
                "Authorization": f"Bearer {token.access_token}",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status_code == 401


def test_dynamic_registration_roundtrips_client_metadata(settings):
    mcp, runtime = create_server(settings)
    with TestClient(create_app(mcp, runtime)) as client:
        response = client.post(
            "/register",
            json={
                "client_name": "Synthetic public client",
                "redirect_uris": ["https://chatgpt.com/oauth/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "application_type": "web",
                "scope": "sellsy.read offline_access",
            },
        )
        assert response.status_code == 201
        metadata = response.json()
        assert metadata["redirect_uris"] == ["https://chatgpt.com/oauth/callback"]
        assert metadata["application_type"] == "web"
        assert metadata["token_endpoint_auth_method"] == "none"
        assert metadata["client_id"]
