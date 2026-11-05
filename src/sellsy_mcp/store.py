from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id TEXT PRIMARY KEY,
  client_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_pending (
  state_hash TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sellsy_grants (
  grant_id TEXT PRIMARY KEY,
  staff_id TEXT NOT NULL,
  staff_email TEXT,
  staff_name TEXT,
  access_token_enc TEXT NOT NULL,
  refresh_token_enc TEXT,
  expires_at INTEGER NOT NULL,
  scope TEXT,
  revoked_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
  token_hash TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  used_at INTEGER
);
CREATE TABLE IF NOT EXISTS mcp_tokens (
  token_hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('access','refresh')),
  client_id TEXT NOT NULL,
  grant_id TEXT NOT NULL,
  scopes_json TEXT NOT NULL,
  resource TEXT,
  expires_at INTEGER NOT NULL,
  revoked_at INTEGER,
  family_id TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS mcp_tokens_grant_idx ON mcp_tokens(grant_id);
CREATE INDEX IF NOT EXISTS mcp_tokens_family_idx ON mcp_tokens(family_id);
CREATE TABLE IF NOT EXISTS search_cursors (
 token_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL, query_hash TEXT NOT NULL,
 state_enc TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS search_cursors_expiry_idx ON search_cursors(expires_at);
CREATE TABLE IF NOT EXISTS manifest_checks (
 manifest_id TEXT PRIMARY KEY, checks_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifest_outcomes (
 manifest_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, outcome_json TEXT NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS manifests (
  manifest_id TEXT PRIMARY KEY,
  grant_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id TEXT,
  payload_json TEXT NOT NULL,
  before_fingerprint TEXT,
  expires_at INTEGER NOT NULL,
  used_at INTEGER,
  created_at INTEGER NOT NULL
);
"""

WRITE_SCHEMAS = {
    "oauth_clients": (
        "client_id",
        ("client_json", "created_at"),
        "INSERT INTO oauth_clients (client_id,client_json,created_at) VALUES (?,?,?) "
        "ON CONFLICT(client_id) DO UPDATE SET client_json=excluded.client_json,"
        "created_at=excluded.created_at",
    ),
    "oauth_pending": (
        "state_hash",
        ("payload_json", "expires_at"),
        "INSERT INTO oauth_pending (state_hash,payload_json,expires_at) VALUES (?,?,?) "
        "ON CONFLICT(state_hash) DO UPDATE SET payload_json=excluded.payload_json,"
        "expires_at=excluded.expires_at",
    ),
    "sellsy_grants": (
        "grant_id",
        (
            "staff_id",
            "staff_email",
            "staff_name",
            "access_token_enc",
            "refresh_token_enc",
            "expires_at",
            "scope",
            "revoked_at",
            "created_at",
            "updated_at",
        ),
        "INSERT INTO sellsy_grants "
        "(grant_id,staff_id,staff_email,staff_name,access_token_enc,refresh_token_enc,expires_at,scope,"
        "revoked_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(grant_id) DO UPDATE SET "
        "staff_id=excluded.staff_id,staff_email=excluded.staff_email,staff_name=excluded.staff_name,"
        "access_token_enc=excluded.access_token_enc,refresh_token_enc=excluded.refresh_token_enc,"
        "expires_at=excluded.expires_at,scope=excluded.scope,revoked_at=excluded.revoked_at,"
        "created_at=excluded.created_at,updated_at=excluded.updated_at",
    ),
    "oauth_codes": (
        "token_hash",
        ("payload_json", "expires_at", "used_at"),
        "INSERT INTO oauth_codes (token_hash,payload_json,expires_at,used_at) VALUES (?,?,?,?) "
        "ON CONFLICT(token_hash) DO UPDATE SET payload_json=excluded.payload_json,"
        "expires_at=excluded.expires_at,used_at=excluded.used_at",
    ),
    "mcp_tokens": (
        "token_hash",
        (
            "kind",
            "client_id",
            "grant_id",
            "scopes_json",
            "resource",
            "expires_at",
            "revoked_at",
            "family_id",
            "created_at",
        ),
        "INSERT INTO mcp_tokens "
        "(token_hash,kind,client_id,grant_id,scopes_json,resource,expires_at,revoked_at,"
        "family_id,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(token_hash) DO UPDATE SET kind=excluded.kind,"
        "client_id=excluded.client_id,grant_id=excluded.grant_id,scopes_json=excluded.scopes_json,"
        "resource=excluded.resource,expires_at=excluded.expires_at,revoked_at=excluded.revoked_at,"
        "family_id=excluded.family_id,created_at=excluded.created_at",
    ),
    "manifests": (
        "manifest_id",
        (
            "grant_id",
            "operation",
            "object_type",
            "object_id",
            "payload_json",
            "before_fingerprint",
            "expires_at",
            "used_at",
            "created_at",
        ),
        "INSERT INTO manifests "
        "(manifest_id,grant_id,operation,object_type,object_id,payload_json,before_fingerprint,expires_at,used_at,"
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(manifest_id) DO UPDATE SET "
        "grant_id=excluded.grant_id,operation=excluded.operation,object_type=excluded.object_type,"
        "object_id=excluded.object_id,payload_json=excluded.payload_json,"
        "before_fingerprint=excluded.before_fingerprint,expires_at=excluded.expires_at,"
        "used_at=excluded.used_at,created_at=excluded.created_at",
    ),
}


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        elif self.path.stat().st_mode & 0o077:
            raise PermissionError(f"Database must be mode 0600: {self.path}")
        with self.connection() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()

    def put_json(self, table: str, key_column: str, key: str, values: dict[str, Any]) -> None:
        expected = WRITE_SCHEMAS.get(table)
        if expected is None or key_column != expected[0] or set(values) != set(expected[1]):
            raise ValueError("Unsupported database write shape")
        with self.connection() as connection:
            connection.execute(expected[2], [key, *(values[field] for field in expected[1])])

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(sql, params).fetchone()
            return dict(row) if row else None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.connection() as connection:
            cursor = connection.execute(sql, params)
            return cursor.rowcount

    def prune(self) -> None:
        now = int(time.time())
        with self.connection() as connection:
            connection.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (now,))
            connection.execute("DELETE FROM oauth_codes WHERE expires_at < ? OR used_at IS NOT NULL", (now,))
            connection.execute(
                "DELETE FROM mcp_tokens WHERE expires_at < ? OR revoked_at IS NOT NULL", (now,)
            )
            connection.execute("DELETE FROM search_cursors WHERE expires_at < ?", (now,))
            connection.execute("DELETE FROM sellsy_grants WHERE revoked_at < ?", (now - 86400 * 90,))
            connection.execute("DELETE FROM manifests WHERE expires_at < ?", (now - 86400 * 30,))
            connection.execute("DELETE FROM manifest_outcomes WHERE updated_at < ?", (now - 86400 * 30,))
            connection.execute(
                "DELETE FROM manifest_checks WHERE manifest_id NOT IN (SELECT manifest_id FROM manifests)"
            )


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def loads(value: str) -> Any:
    return json.loads(value)
