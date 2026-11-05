"""Evidence-based product history. Never combine quotes, deposits and invoices as sales."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from .dates import in_period
from .errors import ToolError, error_result
from .models import ProductHistory
from .presentation import _decimal
from .resolution import company_id_from, matches_product

FAMILIES = ("estimates", "orders", "invoices", "credit-notes")
ACCEPTED = {"accepted", "advanced", "partialinvoiced", "invoiced"}
INVOICED = {"due", "payinprogress", "paid", "late"}
CREDITED = {"stored", "partialspend", "spent"}
NUMERIC_ROWS = {"single", "catalog", "shipping", "packaging"}


def bucket(family: str, document: dict[str, Any]) -> str | None:
    status = document.get("status")
    if family in {"estimates", "orders"}:
        return "accepted_" + family if status in ACCEPTED else None
    if family == "invoices":
        if status not in INVOICED:
            return None
        if document.get("is_deposit"):
            return "deposits"
        if document.get("is_progress_invoice"):
            return "progress_invoices"
        return "invoices"
    if status in CREDITED:
        return "deposit_credit_notes" if document.get("is_deposit") else "credit_notes"
    return None


async def product_history(client: Any, params: ProductHistory) -> dict[str, Any]:
    query = {
        "kind": "product_history",
        **params.model_dump(exclude={"cursor", "document_limit", "max_pages"}),
    }
    if params.cursor:
        state = client.continuations.load(params.cursor, client.grant_id, query)
    else:
        company_id = await company_id_from(client, params.company_id, params.company_name)
        company = await client.get(f"/companies/{company_id}")
        state = {
            "company": {"id": company_id, "name": company.get("name")},
            "family": 0,
            "page_cursor": None,
            "pending": [],
            "page_complete": False,
            "coverage": {},
            "errors": [],
            "quantities": {},
            "seen": [],
            "documents_read": 0,
            "uncomputable_rows": 0,
            "evidence_count": 0,
        }
    pending_evidence = state.pop("pending_evidence", [])
    evidence = pending_evidence[:200]
    state["pending_evidence"] = pending_evidence[200:]
    seen = set(state["seen"])
    read_count = 0
    deadline = time.monotonic() + client.settings.search_timeout_seconds
    while state["family"] < len(FAMILIES) and read_count < params.document_limit and len(evidence) < 200:
        if time.monotonic() >= deadline:
            break
        family = FAMILIES[state["family"]]
        if not state["pending"]:
            if state["page_complete"]:
                state.update(family=state["family"] + 1, page_cursor=None, page_complete=False)
                continue
            filters: dict[str, Any] = {"related_objects": [{"type": "company", "id": state["company"]["id"]}]}
            if family in {"estimates", "invoices"} and (params.date_start or params.date_end):
                filters["date"] = {
                    key: value
                    for key, value in (("start", params.date_start), ("end", params.date_end))
                    if value
                }
            try:
                page = await client.bounded_search(
                    f"/{family}/search",
                    filters=filters,
                    limit=min(100, params.document_limit - read_count),
                    max_pages=params.max_pages,
                    cursor=state["page_cursor"],
                )
            except Exception as exc:
                state["errors"].append({"family": family, **error_result(exc)})
                state.update(family=state["family"] + 1, page_cursor=None, page_complete=False)
                continue
            state["coverage"][family] = page["coverage"]
            state["pending"] = page["data"]
            state["page_cursor"] = page.get("next_cursor")
            state["page_complete"] = not state["page_cursor"]
            if not page["data"] and not state["page_complete"]:
                break
            if not page["data"]:
                continue
        row = state["pending"].pop(0)
        key = f"{family}:{row['id']}"
        if key in seen:
            continue
        seen.add(key)
        read_count += 1
        try:
            document = await client.get(f"/{family}/{row['id']}")
            if document.get("id") != row["id"]:
                raise RuntimeError("Document identity mismatch")
            relations = document.get("related")
            if not isinstance(relations, list) or not any(
                obj.get("type") == "company" and obj.get("id") == state["company"]["id"]
                for obj in relations
                if isinstance(obj, dict)
            ):
                raise ToolError("client_mismatch", "Client du document non vérifié.")
            if not isinstance(document.get("rows"), list):
                raise ToolError("missing_rows", "Lignes du document absentes de la réponse.")
            state["documents_read"] += 1
            if not in_period(document.get("date"), params.date_start, params.date_end):
                continue
            group = bucket(family, document)
            if not group:
                continue
            seen_rows: set[Any] = set()
            for index, line in enumerate(document.get("rows") or []):
                if not isinstance(line, dict) or line.get("type") not in NUMERIC_ROWS:
                    continue
                line_id = line.get("id", index)
                if line_id in seen_rows:
                    continue
                seen_rows.add(line_id)
                if not matches_product(line, params.product_text, params.references, params.item_ids):
                    continue
                quantity = _decimal(line.get("quantity"))
                if quantity is None:
                    state["uncomputable_rows"] += 1
                    continue
                if "credit" in group:
                    quantity = abs(quantity)
                state["quantities"][group] = str(Decimal(state["quantities"].get(group, "0")) + quantity)
                evidence.append(
                    {
                        "document_type": family,
                        "document_id": document["id"],
                        "number": document.get("number"),
                        "date": document.get("date"),
                        "status": document.get("status"),
                        "bucket": group,
                        "row_id": line.get("id"),
                        "reference": line.get("reference"),
                        "description": line.get("description"),
                        "quantity": str(quantity),
                        "unit_amount": line.get("unit_amount"),
                        "currency": document.get("currency"),
                        "is_deposit": bool(document.get("is_deposit")),
                        "is_progress_invoice": bool(document.get("is_progress_invoice")),
                    }
                )
        except Exception as exc:
            state["errors"].append({"family": family, "document_id": row["id"], **error_result(exc)})
    # A last document can consume the budget exactly; still recognize the completed family.
    while state["family"] < len(FAMILIES) and not state["pending"] and state["page_complete"]:
        state.update(family=state["family"] + 1, page_cursor=None, page_complete=False)
    state["pending_evidence"].extend(evidence[200:])
    evidence = evidence[:200]
    state["seen"] = list(seen)
    state["evidence_count"] += len(evidence)
    traversal_done = state["family"] == len(FAMILIES) and not state["pending_evidence"]
    complete = (
        traversal_done
        and not state["errors"]
        and not state["uncomputable_rows"]
        and all(value.get("complete") and not value.get("truncated") for value in state["coverage"].values())
    )
    quantities = state["quantities"]
    invoices_complete = state["coverage"].get("invoices", {}).get("complete", False)
    credits_complete = state["coverage"].get("credit-notes", {}).get("complete", False)
    net_verifiable = (
        traversal_done
        and invoices_complete
        and credits_complete
        and not state["coverage"]["invoices"].get("truncated")
        and not state["coverage"]["credit-notes"].get("truncated")
        and not any(e["family"] in {"invoices", "credit-notes"} for e in state["errors"])
        and not state["uncomputable_rows"]
    )
    next_cursor = None if traversal_done else client.continuations.save(client.grant_id, query, state)
    return {
        "company": state["company"],
        "status": "complete" if complete else "incomplete",
        "query": {
            "product_text": params.product_text,
            "references": params.references,
            "item_ids": params.item_ids,
        },
        "quantities_by_bucket": quantities,
        "net_invoiced_line_quantity": str(
            Decimal(quantities.get("invoices", "0")) - Decimal(quantities.get("credit_notes", "0"))
        )
        if net_verifiable
        else None,
        "active_license_quantity": None,
        "basis": "Quantités des lignes de factures finales moins lignes d'avoirs hors acomptes.",
        "interpretation": [
            "Devis, commandes, acomptes et situations sont séparés : ne pas les additionner.",
            "Les renouvellements restent des ventes distinctes.",
            "Vérifier les relations des devis acceptés : certains peuvent avoir été remplacés.",
            "Une quantité créditée ne prouve pas la désactivation d'une licence.",
            "Les totaux sont cumulés ; les preuves correspondent à cette page de traitement.",
        ],
        "coverage": {
            "complete": complete,
            "truncated": not complete,
            "families": state["coverage"],
            "documents_read": state["documents_read"],
            "evidence_count": state["evidence_count"],
            "uncomputable_rows": state["uncomputable_rows"],
        },
        "errors": state["errors"],
        "evidence": evidence,
        "next_cursor": next_cursor,
        "next_action": (
            "Continuer avec next_cursor et les mêmes critères."
            if next_cursor
            else "Vérifier les droits et erreurs signalés, puis relancer la lecture."
            if state["errors"]
            else None
        ),
    }
