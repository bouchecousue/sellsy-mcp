from sellsy_mcp.config import Settings


def test_new_installation_is_read_only_and_uses_loopback(monkeypatch):
    monkeypatch.delenv("SELLSY_MCP_ENABLE_WRITES", raising=False)
    monkeypatch.delenv("SELLSY_MCP_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("SELLSY_MCP_HOST", raising=False)
    settings = Settings(
        _env_file=None,
        sellsy_client_id="00000000-0000-4000-8000-000000000001",
        sellsy_client_secret="synthetic-test-value",
    )
    assert settings.enable_writes is False
    assert settings.host == "127.0.0.1"
    assert settings.public_base == "http://127.0.0.1:8766"
