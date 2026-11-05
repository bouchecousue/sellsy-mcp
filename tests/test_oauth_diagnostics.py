from __future__ import annotations

import logging

import pytest
import respx
from httpx import Response

from sellsy_mcp.errors import SellsyOAuthError, error_result
from sellsy_mcp.models import OpportunitySearch
from sellsy_mcp.oauth_provider import SellsyOAuthProvider


@pytest.mark.parametrize(
    "body,code",
    [
        ({"error": "invalid_client", "error_description": "secret must not escape"}, "oauth_client_rejected"),
        ({"message": "Client authentication failed"}, "oauth_client_rejected"),
        ({"error": "invalid_grant", "message": "secret must not escape"}, "reconnect_required"),
        ({"error": {"unexpected": "secret must not escape"}}, "reconnect_required"),
    ],
)
def test_oauth_errors_are_actionable_without_upstream_text(body, code):
    with pytest.raises(SellsyOAuthError) as caught:
        SellsyOAuthProvider._raise_upstream_auth_error(Response(401, json=body))
    result = error_result(caught.value)
    assert result["error"]["code"] == code
    assert "secret" not in str(caught.value)
    assert "must not escape" not in str(result)


async def test_capabilities_report_scopes_even_when_oauth_is_broken(runtime_tools):
    runtime, _, tools = runtime_tools
    runtime.provider.store.execute("UPDATE sellsy_grants SET expires_at=0,scope='opportunities.read'")
    with respx.mock as router:
        router.post(str(runtime.provider.settings.sellsy_token_url)).respond(
            401,
            json={"error": "invalid_client"},
        )
        result = await tools["sellsy_get_capabilities"]()
    assert result["upstream"] == "unavailable"
    assert "opportunities.write" in result["missing_scopes"]
    assert not result["opportunity_creation_available"]
    assert not result["opportunity_update_available"]
    assert result["error"]["code"] == "oauth_client_rejected"
    assert result["identity"]["staff_id"] == "42"


async def test_logs_record_identity_and_error_without_inputs(runtime_tools, monkeypatch, caplog):
    from types import SimpleNamespace

    runtime, _, tools = runtime_tools
    monkeypatch.setattr("sellsy_mcp.runtime.get_access_token", lambda: SimpleNamespace(grant_id="alice"))
    runtime.provider.store.execute("UPDATE sellsy_grants SET expires_at=0")
    caplog.set_level(logging.INFO, logger="sellsy_mcp.runtime")
    with respx.mock as router:
        router.post(str(runtime.provider.settings.sellsy_token_url)).respond(
            401,
            json={"error": "invalid_client", "error_description": "sensitive-token-value"},
        )
        result = await tools["sellsy_search_opportunities"](OpportunitySearch(text="private CRM text"))
    assert result["error"]["code"] == "oauth_client_rejected"
    records = [r.message for r in caplog.records if r.name == "sellsy_mcp.runtime"]
    assert any("staff_id=42" in r and "error_code=oauth_client_rejected" in r for r in records)
    assert all("sensitive-token-value" not in r and "private CRM text" not in r for r in records)
