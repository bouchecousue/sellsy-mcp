from __future__ import annotations

from pathlib import Path

import pytest

from sellsy_mcp.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        public_base_url="http://127.0.0.1:8766",
        enable_writes=True,
        sellsy_client_id="00000000-0000-4000-8000-000000000001",
        sellsy_client_secret="sellsy-secret",
        database_path=tmp_path / "state.sqlite3",
        encryption_key_file=tmp_path / "fernet.key",
        audit_log_path=tmp_path / "audit.jsonl",
        mcp_access_token_ttl_seconds=600,
        mcp_refresh_token_ttl_seconds=3600,
        search_page_size=10,
        search_max_pages=2,
    )


@pytest.fixture
def runtime_tools(settings, monkeypatch):
    import time

    from sellsy_mcp.sellsy_client import SellsyClient
    from sellsy_mcp.server import create_server

    mcp, runtime = create_server(settings)
    now = int(time.time())
    runtime.provider.store.put_json(
        "sellsy_grants",
        "grant_id",
        "alice",
        {
            "staff_id": "42",
            "staff_name": "Alice",
            "staff_email": "alice@example.test",
            "access_token_enc": runtime.provider.cipher.encrypt("alice-token"),
            "refresh_token_enc": runtime.provider.cipher.encrypt("alice-refresh"),
            "expires_at": now + 3600,
            "scope": settings.sellsy_scopes,
            "revoked_at": None,
            "created_at": now,
            "updated_at": now,
        },
    )
    client = SellsyClient(settings, runtime.provider, "alice")
    monkeypatch.setattr(runtime, "context", lambda **kwargs: ("alice", client))
    return runtime, client, {tool.name: tool.fn for tool in mcp._tool_manager.list_tools()}
