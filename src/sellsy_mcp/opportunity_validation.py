"""Opportunity creation preconditions and comparison of Sellsy's expanded readback."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import ToolError
from .manifests import fingerprint
from .pagination import normalized, require_complete
from .runtime import Runtime
from .write_validation import mismatches


def require_opportunity_scope(runtime: Runtime, grant_id: str) -> None:
    row = runtime.provider.store.one("SELECT scope FROM sellsy_grants WHERE grant_id=?", (grant_id,))
    if "opportunities.write" not in (row or {}).get("scope", "").split():
        raise ToolError(
            "reconnect_required",
            "Le consentement Sellsy ne permet pas encore de créer ou modifier des opportunités.",
            required_scope="opportunities.write",
            next_action="Reconnecter l'application Sellsy puis préparer un nouveau manifeste.",
        )


def has_relation(obj: dict[str, Any], kind: str, object_id: int) -> bool:
    return any(r.get("type") == kind and r.get("id") == object_id for r in obj.get("related", []))


async def validate_opportunity_references(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    company_id = payload["related"][0]["id"]
    company = await client.get(f"/companies/{company_id}")
    if company.get("id") != company_id or company.get("is_archived"):
        raise ToolError("invalid_company", "Le client est archivé ou son identité n'est pas vérifiée.")
    pipelines = await client.list_all("/opportunities/pipelines", limit=5000, max_pages=100)
    require_complete(pipelines, "pipelines")
    pipeline = next((p for p in pipelines["data"] if p.get("id") == payload["pipeline"]), None)
    if pipeline is None:
        raise ToolError("invalid_pipeline", "Pipeline absent de la liste actuelle.")
    steps = await client.list_all(
        f"/opportunities/pipelines/{payload['pipeline']}/steps", limit=5000, max_pages=100
    )
    require_complete(steps, "étapes du pipeline")
    step = next((s for s in steps["data"] if s.get("id") == payload["step"]), None)
    if step is None:
        raise ToolError("invalid_step", "L'étape n'appartient pas au pipeline choisi.")
    if payload.get("contact_ids"):
        contacts = await client.list_all(f"/companies/{company_id}/contacts", limit=5000, max_pages=100)
        require_complete(contacts, "contacts du client")
        if set(payload["contact_ids"]) - {c["id"] for c in contacts["data"] if not c.get("is_archived")}:
            raise ToolError("contact_company_mismatch", "Un contact n'est pas rattaché à ce client.")
    staff_ids = set(payload.get("assigned_staff_ids") or [])
    if payload.get("owner_id"):
        staff_ids.add(payload["owner_id"])
    if staff_ids:
        staffs = await client.list_all("/staffs", limit=5000, max_pages=100)
        require_complete(staffs, "collaborateurs")
        active = {s["id"] for s in staffs["data"] if s.get("status") == "ok"}
        if staff_ids - active:
            raise ToolError("invalid_staff", "Collaborateur absent ou inactif.")
    return {
        "company": {"id": company["id"], "name": company.get("name")},
        "pipeline": {"id": pipeline["id"], "name": pipeline.get("name")},
        "step": {"id": step["id"], "name": step.get("name")},
    }


async def validate_opportunity_creation(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    references = await validate_opportunity_references(client, payload)
    company_id = payload["related"][0]["id"]
    estimate = None
    if payload.get("main_doc_id"):
        estimate = await client.get(f"/estimates/{payload['main_doc_id']}")
        if estimate.get("id") != payload["main_doc_id"] or not has_relation(estimate, "company", company_id):
            raise ToolError("document_company_mismatch", "Le devis n'appartient pas à ce client.")
        if any(r.get("type") == "opportunity" for r in estimate.get("related", [])):
            raise ToolError("document_already_linked", "Le devis est déjà lié à une opportunité.")
    existing = await client.bounded_search(
        "/opportunities/search",
        filters={"related_objects": [{"type": "company", "id": company_id}]},
        limit=5000,
        max_pages=100,
    )
    require_complete(existing, "contrôle des doublons d'opportunité")
    duplicates = [
        {"id": o.get("id"), "number": o.get("number"), "name": o.get("name"), "status": o.get("status")}
        for o in existing["data"]
        if normalized(o.get("name")) == normalized(payload["name"])
        or (payload.get("main_doc_id") and o.get("main_doc_id") == payload["main_doc_id"])
    ]
    if duplicates:
        raise ToolError(
            "duplicate_opportunity", "Une opportunité correspondante existe déjà.", candidates=duplicates
        )
    # Sellsy validates the current payload without creating an object (204).
    await client.request("POST", "/opportunities", params={"verify": "true"}, json=payload)
    return {
        **references,
        "duplicate_check": existing["coverage"],
        "estimate_fingerprint": fingerprint(estimate) if estimate else None,
    }


def opportunity_snapshot(live: dict[str, Any]) -> dict[str, Any]:
    """Writable fields in API input shape, plus relationships preserved on update."""
    company_id = live.get("company_id")
    if not company_id or not has_relation(live, "company", company_id):
        raise ToolError("unsupported_customer", "La mise à jour nécessite une société liée vérifiée.")
    result = {
        k: live[k] for k in ("name", "status", "note", "probability", "due_date", "main_doc_id") if k in live
    }
    result["related"] = [{"type": "company", "id": company_id}]
    for key in ("pipeline", "step"):
        value = (live.get(key) or {}).get("id")
        if not value:
            raise ToolError("invalid_response", "Pipeline ou étape absent de l'opportunité relue.")
        result[key] = value
    owner = live.get("owner") or {}
    if not owner.get("id") or owner.get("type") != "staff" or not live.get("status"):
        raise ToolError("unsupported_owner", "Le responsable et le statut doivent pouvoir être conservés.")
    result["owner_id"] = owner["id"]
    for key in ("contact_ids", "assigned_staff_ids"):
        if key in live:
            result[key] = live[key] or []
    if live.get("amount") is not None:
        result["amount"] = str(live["amount"]["value"])
    return result


async def validate_opportunity_update(
    client: Any, object_id: int, payload: dict[str, Any], expected: dict[str, Any]
) -> dict[str, Any]:
    references = await validate_opportunity_references(client, expected)
    await client.request("PUT", f"/opportunities/{object_id}", params={"verify": "true"}, json=payload)
    return references


def opportunity_mismatches(payload: dict[str, Any], live: dict[str, Any]) -> list[str]:
    expected = {k: v for k, v in payload.items() if k not in {"amount", "contact_ids", "assigned_staff_ids"}}
    expected["pipeline"] = {"id": payload["pipeline"]}
    expected["step"] = {"id": payload["step"]}
    expected["company_id"] = payload["related"][0]["id"]
    if "owner_id" in expected:
        expected["owner"] = {"id": expected.pop("owner_id"), "type": "staff"}
    # Related contacts may be expanded by Sellsy; company and contacts are checked independently.
    del expected["related"]
    issues = mismatches(expected, live)
    if not has_relation(live, "company", payload["related"][0]["id"]):
        issues.append("related.company")
    for key in ("contact_ids", "assigned_staff_ids"):
        if key in payload and sorted(payload[key]) != sorted(live.get(key) or []):
            issues.append(key)
    if "amount" in payload:
        try:
            actual = (live.get("amount") or {}).get("value")
            if Decimal(payload["amount"]) != Decimal(str(actual)):
                issues.append("amount.value")
        except (InvalidOperation, AttributeError):
            issues.append("amount.value")
    return issues
