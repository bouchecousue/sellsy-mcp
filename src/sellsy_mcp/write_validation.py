from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import ToolError
from .pagination import require_complete

ROW_FIELDS = {
    "id",
    "type",
    "reference",
    "description",
    "quantity",
    "unit_amount",
    "purchase_amount",
    "tax_id",
    "related",
    "discount",
    "unit_id",
    "parent",
    "is_printable",
    "is_totaled",
}
DOCUMENT_FIELDS = {
    "id",
    "status",
    "subject",
    "related",
    "contact_id",
    "currency",
    "invoicing_address_id",
    "delivery_address_id",
    "company_reference",
}
DECIMALS = {"quantity", "unit_amount", "purchase_amount"}
TRANSITIONS = {
    "draft": {"sent", "cancelled"},
    "sent": {"draft", "accepted", "refused", "cancelled"},
    "read": {"draft", "accepted", "refused", "cancelled"},
    "refused": {"draft"},
    "expired": {"draft", "sent", "cancelled"},
    "accepted": {"cancelled"},
}


def snapshot(document: dict[str, Any]) -> dict[str, Any]:
    return {
        **{k: v for k, v in document.items() if k in DOCUMENT_FIELDS},
        "rows": [{k: v for k, v in row.items() if k in ROW_FIELDS} for row in document.get("rows", [])],
    }


def mismatches(expected: Any, actual: Any, path: str = "") -> list[str]:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [path]
        return [
            issue
            for key, value in expected.items()
            for issue in mismatches(value, actual.get(key), f"{path}.{key}".strip("."))
        ]
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return [path + ".count"]
        if expected and all(isinstance(row, dict) and row.get("id") for row in expected):
            actual_by_id = {str(row.get("id")): row for row in actual if isinstance(row, dict)}
            return [
                issue
                for row in expected
                for issue in mismatches(row, actual_by_id.get(str(row["id"])), f"{path}[{row['id']}]")
            ]
        return [
            issue
            for i, (a, b) in enumerate(zip(expected, actual, strict=True))
            for issue in mismatches(a, b, f"{path}[{i}]")
        ]
    if path.split(".")[-1] in DECIMALS:
        try:
            if Decimal(str(expected)) == Decimal(str(actual)):
                return []
        except InvalidOperation:
            pass
    return [] if expected == actual else [path]


async def validate_taxes(client: Any, rows: list[dict[str, Any]]) -> None:
    ids = {int(row["tax_id"]) for row in rows if row.get("tax_id") is not None}
    if not ids:
        return
    taxes = await client.list_all("/taxes", limit=1000, max_pages=50)
    require_complete(taxes, "taxes actives")
    active = {int(tax["id"]) for tax in taxes["data"] if tax.get("is_active") is True}
    if ids - active:
        raise ToolError("invalid_tax", "Taxe absente ou inactive.", tax_ids=sorted(ids - active))


async def validate_creation(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    company_id = payload["related"][0]["id"]
    company = await client.get(f"/companies/{company_id}")
    if company.get("is_archived"):
        raise ToolError("archived_company", "Le client est archivé.")
    if payload.get("contact_id"):
        contacts = await client.list_all(f"/companies/{company_id}/contacts", limit=5000, max_pages=100)
        require_complete(contacts, "contacts du client")
        if payload["contact_id"] not in {row["id"] for row in contacts["data"]}:
            raise ToolError("contact_company_mismatch", "Le contact n'est pas rattaché à ce client.")
    if payload.get("invoicing_address_id") or payload.get("delivery_address_id"):
        addresses = await client.list_all(f"/companies/{company_id}/addresses", limit=5000, max_pages=100)
        require_complete(addresses, "adresses du client")
        address_ids = {row["id"] for row in addresses["data"]}
        for name in ("invoicing_address_id", "delivery_address_id"):
            if payload.get(name) is not None and payload[name] not in address_ids:
                raise ToolError(
                    "address_company_mismatch", "Adresse non vérifiée pour ce client.", field=name
                )
    await validate_taxes(client, payload["rows"])
    for row in payload["rows"]:
        if row.get("type") == "catalog":
            related = row["related"]
            item = await client.get(f"/items/{related['id']}")
            if item.get("is_archived") or item.get("type") != related["type"]:
                raise ToolError("invalid_catalog_item", "Article archivé ou type incohérent.")
    return company


def validate_transition(before: str, after: str) -> None:
    if after not in TRANSITIONS.get(before, set()):
        raise ToolError(
            "unsupported_transition",
            "Transition de statut non autorisée par ce MCP.",
            before=before,
            after=after,
            allowed=sorted(TRANSITIONS.get(before, set())),
        )
