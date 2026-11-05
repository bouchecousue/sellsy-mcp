from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from sellsy_mcp.server import create_app, create_server

EXPECTED_TOOLS = {
    "sellsy_prepare_opportunity_update",
    "sellsy_apply_opportunity_update",
    "sellsy_prepare_opportunity_create",
    "sellsy_apply_opportunity_create",
    "sellsy_list_company_contacts",
    "sellsy_search_native",
    "sellsy_get_customer_product_history",
    "sellsy_search_records",
    "sellsy_get_record",
    "sellsy_get_linked_data",
    "sellsy_search_subscription_installments",
    "sellsy_read_file",
    "sellsy_get_capabilities",
    "sellsy_get_manifest_result",
    "sellsy_search_estimates",
    "sellsy_get_estimate",
    "sellsy_search_companies",
    "sellsy_get_company",
    "sellsy_search_contacts",
    "sellsy_get_contact",
    "sellsy_search_items",
    "sellsy_get_item",
    "sellsy_search_subscriptions",
    "sellsy_get_subscription",
    "sellsy_search_opportunities",
    "sellsy_get_opportunity",
    "sellsy_search_invoices",
    "sellsy_get_invoice",
    "sellsy_search_deposit_invoices",
    "sellsy_get_deposit_invoice",
    "sellsy_search_comments",
    "sellsy_list_related_files",
    "sellsy_list_related_payments",
    "sellsy_analyze_commercial_document",
    "sellsy_get_customer_context",
    "sellsy_search_workspace",
    "sellsy_prepare_estimate_create",
    "sellsy_apply_estimate_create",
    "sellsy_prepare_estimate_update",
    "sellsy_apply_estimate_update",
    "sellsy_prepare_estimate_status_update",
    "sellsy_apply_estimate_status_update",
}


def test_server_surface_and_oauth_discovery(settings):
    mcp, runtime = create_server(settings)
    assert {tool.name for tool in mcp._tool_manager.list_tools()} == EXPECTED_TOOLS
    app = create_app(mcp, runtime)
    headers = {"host": "127.0.0.1:8766"}
    with TestClient(app) as client:
        metadata = client.get("/.well-known/oauth-authorization-server", headers=headers)
        assert metadata.status_code == 200
        assert metadata.json()["authorization_endpoint"].endswith("/authorize")
        assert metadata.json()["registration_endpoint"].endswith("/register")
        assert metadata.json()["issuer"] == str(settings.public_base_url)
        assert "offline_access" in metadata.json()["scopes_supported"]
        prm = client.get("/.well-known/oauth-protected-resource/mcp", headers=headers)
        assert prm.status_code == 200
        assert prm.json()["resource"].endswith("/mcp")
        assert prm.json()["authorization_servers"] == [metadata.json()["issuer"]]
        challenge = client.post(
            "/mcp",
            headers={**headers, "content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert challenge.status_code == 401
        assert "resource_metadata" in challenge.headers["www-authenticate"]
        health = client.get("/health/ready", headers=headers).json()
        assert health["status"] == "ready"
        assert health["version"] == "0.5.0b2"
        assert health["tools_count"] == len(EXPECTED_TOOLS)
        assert len(health["surface_sha256"]) == 64
        assert health["last_maintenance"]


@pytest.mark.parametrize("enable_writes", [True, False])
def test_resource_discovery_advertises_optional_scopes_without_requiring_writes(settings, enable_writes):
    settings.enable_writes = enable_writes
    mcp, runtime = create_server(settings)
    app = create_app(mcp, runtime)
    assert mcp.settings.auth.required_scopes == ["sellsy.read"]
    with TestClient(app) as client:
        response = client.get("/.well-known/oauth-protected-resource/mcp")
        assert response.status_code == 200
        metadata = response.json()
        assert metadata["resource"] == settings.resource_url
        assert "sellsy.read" in metadata["scopes_supported"]
        assert "offline_access" in metadata["scopes_supported"]
        assert ("sellsy.write" in metadata["scopes_supported"]) is enable_writes
        preflight = client.options(
            "/.well-known/oauth-protected-resource/mcp",
            headers={"Origin": "https://chatgpt.com", "Access-Control-Request-Method": "GET"},
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "*"


def test_authenticated_mcp_transport_lists_new_tools_and_runs_native_search(runtime_tools, settings):
    import respx

    runtime, _, _ = runtime_tools
    tokens = runtime.provider._issue_mcp_tokens(
        client_id="transport-test", grant_id="alice", scopes=["sellsy.read"], resource=settings.resource_url
    )
    mcp, http_runtime = create_server(settings)
    headers = {
        "host": "127.0.0.1:8766",
        "Authorization": f"Bearer {tokens.access_token}",
        "Accept": "application/json, text/event-stream",
    }
    with respx.mock as router, TestClient(create_app(mcp, http_runtime)) as client:
        router.get("https://api.sellsy.com/v2/search").respond(
            200,
            json={
                "data": [
                    {
                        "id": "search-id",
                        "name": "EXAMPLE PHARMACIENS",
                        "object": {"id": 1001, "type": "company"},
                    }
                ],
                "pagination": {"total": 1},
            },
        )
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert response.status_code == 200
        response = client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        assert {t["name"] for t in response.json()["result"]["tools"]} == EXPECTED_TOOLS
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "sellsy_search_native",
                    "arguments": {"params": {"text": "EXAMPLE", "object_types": ["company"]}},
                },
            },
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert not result["isError"], result
        assert result["structuredContent"]["data"][0]["id"] == 1001
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "sellsy_apply_estimate_create",
                    "arguments": {"params": {"manifest_id": "x" * 30, "approved": True}},
                },
            },
        )
        assert response.json()["result"]["structuredContent"]["error"]["code"] == "permission_denied"
