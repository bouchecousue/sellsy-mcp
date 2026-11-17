from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any

from .errors import ToolError
from .models import EstimateCreateRow, EstimateUpdate
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
    "text",
    "is_optional",
    "eco_tax",
    "sku_id",
    "warehouse_id",
    "accounting_code_id",
    "analytic_code",
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
        "rows": [{k: v for k, v in row.items() if k in ROW_FIELDS} for row in (document.get("rows") or [])],
    }


class LineBreaks(HTMLParser):
    """Locate equivalent self-closing line break tags without rewriting other HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.replacements: list[tuple[int, int, int]] = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text()
        if tag == "br" and not attrs and raw in {"<br/>", "<br />"}:
            line, column = self.getpos()
            self.replacements.append((line - 1, column, len(raw)))


def canonical_line_breaks(description: str) -> str:
    if "<br/>" not in description and "<br />" not in description:
        return description
    parser = LineBreaks()
    try:
        parser.feed(description)
        parser.close()
    except AssertionError:
        return description
    lines = description.split("\n")
    for line, column, length in reversed(parser.replacements):
        text = lines[line]
        lines[line] = text[:column] + "<br>" + text[column + length :]
    return "\n".join(lines)


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
    if (
        path.startswith("rows[")
        and path.endswith("].description")
        and isinstance(expected, str)
        and isinstance(actual, str)
    ):
        return [] if canonical_line_breaks(expected) == canonical_line_breaks(actual) else [path]
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
    await validate_estimate_rows(client, payload["rows"])
    return company


async def catalog_item_type(client: Any, item_id: int, expected_type: str | None) -> str:
    item = await client.get(f"/items/{item_id}")
    kind = item.get("type")
    if (
        item.get("id") != item_id
        or item.get("is_archived")
        or kind not in {"product", "service", "packaging", "shipping"}
        or (expected_type is not None and kind != expected_type)
    ):
        raise ToolError("invalid_catalog_item", "Article archivé, inconnu ou type incohérent.")
    return kind


async def validate_estimate_rows(client: Any, rows: list[dict[str, Any]]) -> None:
    await validate_taxes(client, rows)
    for row in rows:
        if row.get("type") == "catalog":
            await catalog_item_type(client, row["related"]["id"], row["related"]["type"])


async def new_estimate_rows(
    client: Any, rows: list[EstimateCreateRow], *, catalog_types: tuple[str, ...]
) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        values = row.model_dump(exclude_none=True, exclude={"item_id", "related_type"})
        if row.type == "catalog":
            kind = await catalog_item_type(client, row.item_id, row.related_type)
            if kind not in catalog_types:
                raise ValueError(f"Type d’article incompatible avec cette ligne catalogue : {kind}")
            values["related"] = {"id": row.item_id, "type": kind}
        result.append(values)
    return result


async def estimate_update_plan(
    client: Any, live: dict[str, Any], params: EstimateUpdate
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if (
        live.get("id") != params.estimate_id
        or "rows" not in live
        or not isinstance(live["rows"], list | None)
    ):
        raise ToolError("invalid_response", "Impossible de vérifier le devis et ses lignes.")
    if live.get("status") != "draft":
        raise ToolError("not_editable", "Seuls les devis brouillons sont modifiables.")
    expected = snapshot(live)
    current = {row.get("id"): row for row in expected["rows"]}
    if len(current) != len(expected["rows"]) or any(type(i) is not int or i <= 0 for i in current):
        raise ToolError("invalid_response", "Impossible d’identifier chaque ligne du devis.")
    patches = {row.id: row for row in params.rows}
    removed = set(params.remove_row_ids)
    unknown = (patches.keys() | removed) - current.keys()
    if unknown:
        raise ValueError(f"Lignes inconnues : {sorted(unknown)}")

    payload: dict[str, Any] = {}
    diff = []
    if params.subject is not None and params.subject != live.get("subject"):
        diff.append({"field": "subject", "before": live.get("subject"), "after": params.subject})
        payload["subject"] = expected["subject"] = params.subject

    if patches or removed or params.add_rows:
        wire_rows = []
        kept = []
        for row_id, row in current.items():
            if row_id in removed:
                diff.append({"operation": "remove", "row_id": row_id, "before": row})
                continue
            values = {"id": row_id}
            if row_id in patches:
                values.update(patches[row_id].model_dump(exclude_none=True))
                changes = {
                    key: {"before": row.get(key), "after": value}
                    for key, value in values.items()
                    if key != "id" and mismatches({key: value}, row)
                }
                if not changes:
                    raise ValueError(f"Aucune modification effective sur la ligne {row_id}")
                if row.get("description") is not None:
                    values.setdefault("description", row["description"])
                row.update(values)
                diff.append({"row_id": row_id, "changes": changes})
            kept.append(row)
            wire_rows.append(values)
        additions = await new_estimate_rows(client, params.add_rows, catalog_types=("product", "service"))
        diff.extend({"operation": "add", "after": row} for row in additions)
        expected["rows"] = kept + additions
        payload["rows"] = wire_rows + additions
        await validate_taxes(client, payload["rows"])
    if not diff:
        raise ValueError("Aucune modification effective du devis.")
    return payload, expected, diff


def estimate_update_mismatches(expected: dict, actual: dict, before: dict) -> list[str]:
    issues = mismatches({k: v for k, v in expected.items() if k != "rows"}, actual)
    actual_rows = [] if "rows" in actual and actual["rows"] is None else actual.get("rows")
    if not isinstance(actual_rows, list) or len(expected["rows"]) != len(actual_rows):
        return [*issues, "rows.count"]
    original_ids = {row["id"] for row in (before["rows"] or [])}
    seen = set()
    for i, (wanted, found) in enumerate(zip(expected["rows"], actual_rows, strict=True)):
        path = f"rows[{i}]"
        row_id = found.get("id") if isinstance(found, dict) else None
        if type(row_id) is not int or row_id <= 0 or row_id in seen:
            issues.append(f"{path}.id")
            continue
        if "id" not in wanted and row_id in original_ids:
            issues.append(f"{path}.id")
        seen.add(row_id)
        issues.extend(mismatches(wanted, found, path))
    return issues


def validate_transition(before: str, after: str) -> None:
    if after not in TRANSITIONS.get(before, set()):
        raise ToolError(
            "unsupported_transition",
            "Transition de statut non autorisée par ce MCP.",
            before=before,
            after=after,
            allowed=sorted(TRANSITIONS.get(before, set())),
        )
