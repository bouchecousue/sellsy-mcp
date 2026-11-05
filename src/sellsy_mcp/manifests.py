from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from .config import Settings
from .store import Store, dumps, loads


def fingerprint(value: Any) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()


CHANGE_SECTIONS = ("identity_status", "rows", "relations", "commercial_header", "other_fields")


def section_fingerprints(document: dict[str, Any]) -> dict[str, str]:
    """Internal diagnostics only: fixed categories, never values or caller-supplied field names."""
    groups = {name: {} for name in CHANGE_SECTIONS}
    for key, value in document.items():
        if key in {"id", "status"}:
            section = "identity_status"
        elif key == "rows":
            section = "rows"
        elif key in {"related", "contact_id", "owner_id", "assigned_staff_ids", "main_doc_id"}:
            section = "relations"
        elif key in {
            "subject",
            "name",
            "note",
            "amount",
            "currency",
            "due_date",
            "pipeline",
            "step",
            "invoicing_address_id",
            "delivery_address_id",
            "company_reference",
        }:
            section = "commercial_header"
        else:
            section = "other_fields"
        groups[section][key] = value
    return {name: fingerprint(value) for name, value in groups.items()}


def changed_sections(checks: dict[str, Any], document: dict[str, Any]) -> list[str]:
    previous = checks.get("before_sections")
    if not isinstance(previous, dict) or set(previous) != set(CHANGE_SECTIONS):
        return ["unavailable"]  # Manifests prepared before this diagnostic remain valid.
    current = section_fingerprints(document)
    return [name for name in CHANGE_SECTIONS if previous[name] != current[name]]


class ManifestStore:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings

    def create(
        self,
        *,
        grant_id: str,
        operation: str,
        object_type: str,
        object_id: str | None,
        payload: dict[str, Any],
        before_fingerprint: str | None,
        checks: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        manifest_id = f"mf_{secrets.token_urlsafe(24)}"
        expires_at = now + self.settings.manifest_ttl_seconds
        self.store.put_json(
            "manifests",
            "manifest_id",
            manifest_id,
            {
                "grant_id": grant_id,
                "operation": operation,
                "object_type": object_type,
                "object_id": object_id,
                "payload_json": dumps(payload),
                "before_fingerprint": before_fingerprint,
                "expires_at": expires_at,
                "used_at": None,
                "created_at": now,
            },
        )
        self.store.execute(
            "INSERT INTO manifest_checks(manifest_id,checks_json) VALUES(?,?)",
            (manifest_id, dumps(checks or {})),
        )
        return {
            "manifest_id": manifest_id,
            "operation": operation,
            "object_type": object_type,
            "object_id": object_id,
            "payload": payload,
            "before_fingerprint": before_fingerprint,
            "expires_at": expires_at,
            "requires_explicit_approval": True,
        }

    def inspect(self, manifest_id: str, grant_id: str, operation: str | None = None) -> dict[str, Any]:
        row = self.store.one("SELECT * FROM manifests WHERE manifest_id = ?", (manifest_id,))
        now = int(time.time())
        if not row or row["grant_id"] != grant_id:
            raise PermissionError("Manifest not found for this Sellsy user")
        if row["used_at"] is not None:
            raise ValueError("Manifest was already used")
        if row["expires_at"] < now:
            raise ValueError("Manifest expired; prepare the operation again")
        if operation is not None and row["operation"] != operation:
            raise ValueError("Manifest operation does not match this tool")
        row["payload"] = loads(row.pop("payload_json"))
        checks = self.store.one("SELECT checks_json FROM manifest_checks WHERE manifest_id=?", (manifest_id,))
        row["checks"] = loads(checks["checks_json"]) if checks else {}
        return row

    def claim(self, manifest_id: str, grant_id: str, operation: str | None = None) -> dict[str, Any]:
        row = self.inspect(manifest_id, grant_id, operation)
        now = int(time.time())
        claimed = self.store.execute(
            "UPDATE manifests SET used_at = ? WHERE manifest_id = ? AND used_at IS NULL",
            (now, manifest_id),
        )
        if claimed != 1:
            raise ValueError("Manifest was already claimed")
        return row

    def outcome(self, manifest_id: str, grant_id: str, result: dict[str, Any]) -> None:
        self.store.execute(
            "INSERT INTO manifest_outcomes(manifest_id,grant_id,outcome_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(manifest_id) DO UPDATE SET outcome_json=excluded.outcome_json,"
            "updated_at=excluded.updated_at",
            (manifest_id, grant_id, dumps(result), int(time.time())),
        )

    def read_outcome(self, manifest_id: str, grant_id: str) -> dict[str, Any]:
        row = self.store.one(
            "SELECT * FROM manifest_outcomes WHERE manifest_id=? AND grant_id=?", (manifest_id, grant_id)
        )
        if row:
            return loads(row["outcome_json"])
        self.inspect(manifest_id, grant_id)
        return {"status": "prepared", "write_attempted": False}
