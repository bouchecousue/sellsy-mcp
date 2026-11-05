from __future__ import annotations

import stat

from sellsy_mcp.crypto import TokenCipher
from sellsy_mcp.manifests import ManifestStore, fingerprint
from sellsy_mcp.store import Store


def test_cipher_and_database_are_private(settings):
    settings.prepare_paths()
    cipher = TokenCipher(settings.encryption_key_file)
    store = Store(settings.database_path)

    encrypted = cipher.encrypt("top-secret")
    assert "top-secret" not in encrypted
    assert cipher.decrypt(encrypted) == "top-secret"
    assert stat.S_IMODE(settings.encryption_key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.database_path.stat().st_mode) == 0o600
    assert store.one("SELECT 1 AS ok") == {"ok": 1}


def test_manifest_is_user_bound_and_one_shot(settings):
    store = Store(settings.database_path)
    manifests = ManifestStore(store, settings)
    before = {"id": 12, "status": "draft"}
    created = manifests.create(
        grant_id="grant-a",
        operation="estimate.update",
        object_type="estimate",
        object_id="12",
        payload={"rows": [{"id": 1, "quantity": "2"}]},
        before_fingerprint=fingerprint(before),
    )

    try:
        manifests.claim(created["manifest_id"], "grant-b")
    except PermissionError:
        pass
    else:
        raise AssertionError("Another grant must not claim the manifest")

    claimed = manifests.claim(created["manifest_id"], "grant-a")
    assert claimed["payload"]["rows"][0]["quantity"] == "2"
    try:
        manifests.claim(created["manifest_id"], "grant-a")
    except ValueError as exc:
        assert "already" in str(exc)
    else:
        raise AssertionError("A manifest must be one-shot")
