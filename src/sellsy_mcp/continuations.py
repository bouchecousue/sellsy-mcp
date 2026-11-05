"""Encrypted, expiring, user-bound continuation state; opaque tokens in MCP output."""

from __future__ import annotations

import secrets
import time
from typing import Any

from .crypto import TokenCipher
from .errors import ToolError
from .manifests import fingerprint
from .store import Store, dumps, loads, token_hash


class Continuations:
    def __init__(self, store: Store, cipher: TokenCipher, ttl: int = 1800):
        self.store, self.cipher, self.ttl = store, cipher, ttl

    def save(self, grant_id: str, query: Any, state: dict[str, Any]) -> str:
        token = "sc_" + secrets.token_urlsafe(32)
        self.store.execute(
            "INSERT INTO search_cursors(token_hash,grant_id,query_hash,state_enc,expires_at) "
            "VALUES(?,?,?,?,?)",
            (
                token_hash(token),
                grant_id,
                fingerprint(query),
                self.cipher.encrypt(dumps(state)),
                int(time.time()) + self.ttl,
            ),
        )
        return token

    def load(self, token: str, grant_id: str, query: Any) -> dict[str, Any]:
        row = self.store.one("SELECT * FROM search_cursors WHERE token_hash=?", (token_hash(token),))
        if (
            not row
            or row["grant_id"] != grant_id
            or row["query_hash"] != fingerprint(query)
            or row["expires_at"] <= time.time()
        ):
            raise ToolError(
                "invalid_cursor",
                "Curseur expiré ou incompatible avec cette recherche.",
                next_action="Relancer la recherche sans cursor.",
            )
        return loads(self.cipher.decrypt(row["state_enc"]))
