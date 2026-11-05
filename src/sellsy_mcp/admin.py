"""Local administration. Never expose secrets or administrative mutations through MCP."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from .audit import AuditLog
from .config import Settings
from .crypto import TokenCipher
from .store import Store


def revoke_grant(store: Store, grant_id: str, audit: AuditLog) -> dict:
    now = int(time.time())
    with store.connection() as conn:
        grant = conn.execute("SELECT staff_id FROM sellsy_grants WHERE grant_id=?", (grant_id,)).fetchone()
        if not grant:
            raise ValueError("Unknown grant ID")
        conn.execute(
            "UPDATE sellsy_grants SET revoked_at=?, access_token_enc='', refresh_token_enc=NULL, "
            "updated_at=? WHERE grant_id=?",
            (now, now, grant_id),
        )
        conn.execute("UPDATE mcp_tokens SET revoked_at=? WHERE grant_id=?", (now, grant_id))
        conn.execute("DELETE FROM search_cursors WHERE grant_id=?", (grant_id,))
        conn.execute(
            "UPDATE manifests SET expires_at=? WHERE grant_id=? AND used_at IS NULL", (now - 1, grant_id)
        )
    audit.write({"tool": "admin.revoke_grant", "staff_id": grant["staff_id"], "result": "revoked"})
    return {
        "grant_id": grant_id,
        "status": "revoked_locally",
        "note": "Tokens invalidés. Secrets amont supprimés. Retirer aussi le consentement Sellsy au besoin.",
    }


def encrypted_backup(store: Store, cipher: TokenCipher, destination: Path) -> None:
    # SQLite backup API includes WAL safely, unlike a raw database file copy.
    with tempfile.TemporaryDirectory() as directory:
        snapshot = Path(directory) / "state.sqlite3"
        with store.connection() as source, sqlite3.connect(snapshot) as target:
            source.backup(target)
        payload = json.dumps(
            {
                "format": 1,
                "created_at": int(time.time()),
                "database": base64.b64encode(snapshot.read_bytes()).decode(),
            }
        )
        with destination.open("xb") as output:
            os.chmod(destination, 0o600)
            output.write(cipher.encrypt(payload).encode())


def restore_backup(cipher: TokenCipher, backup: Path, destination: Path) -> None:
    if destination.exists():
        raise ValueError("Restore destination must not exist")
    payload = json.loads(cipher.decrypt(backup.read_text()))
    if payload.get("format") != 1:
        raise ValueError("Unsupported backup format")
    contents = base64.b64decode(payload["database"], validate=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        restored = Path(directory) / "restored.sqlite3"
        restored.write_bytes(contents)
        restored.chmod(0o600)
        with sqlite3.connect(restored) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Invalid backup database")
        # Exclusive hard link refuses a race replacing an existing production database.
        os.link(restored, destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("grants", help="List identities and scopes, without tokens")
    sub.add_parser("prune")
    reauthorize = sub.add_parser("reauthorize", help="Start consent renewal for this same staff identity")
    reauthorize.add_argument("--grant-id", required=True)
    reauthorize.add_argument(
        "--add-write-scope",
        action="append",
        default=[],
        help="Explicitly request this configured write scope during the collaborator's Sellsy consent",
    )
    revoke = sub.add_parser("revoke")
    revoke.add_argument("--grant-id", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings()
    cipher = TokenCipher(settings.encryption_key_file)
    if args.command == "restore":
        restore_backup(cipher, args.input, args.output)
        print(json.dumps({"status": "restored", "path": str(args.output)}))
        return
    store = Store(settings.database_path)
    if args.command == "grants":
        with store.connection() as conn:
            rows = conn.execute(
                "SELECT grant_id,staff_id,staff_name,scope,expires_at,revoked_at "
                "FROM sellsy_grants ORDER BY updated_at DESC"
            ).fetchall()
        print(json.dumps([dict(row) for row in rows], ensure_ascii=False))
    elif args.command == "reauthorize":
        from .oauth_provider import SellsyOAuthProvider

        provider = SellsyOAuthProvider(settings, store, cipher)
        print(provider.reauthorize_grant(args.grant_id, add_write_scopes=tuple(args.add_write_scope)))
    elif args.command == "revoke":
        print(json.dumps(revoke_grant(store, args.grant_id, AuditLog(settings.audit_log_path))))
    elif args.command == "prune":
        store.prune()
        print('{"status":"pruned"}')
    elif args.command == "backup":
        encrypted_backup(store, cipher, args.output)
        print(json.dumps({"status": "backed_up", "path": str(args.output)}))


if __name__ == "__main__":
    main()
