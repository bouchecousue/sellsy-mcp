"""Native discovery, explicit fallback, and conservative identity resolution."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from .errors import ToolError
from .pagination import normalized, require_complete, text_matches

NATIVE_TYPES = {
    "companies": ["company"],
    "contacts": ["contact"],
    "items": ["item"],
    "staffs": ["staff"],
    "estimates": ["document.estimate"],
    "invoices": ["document.invoice"],
    "credit-notes": ["document.creditnote"],
    "orders": ["document.order"],
    "deliveries": ["document.delivery"],
    "opportunities": ["opportunity"],
}
ENTITY_FIELDS = {
    "companies": ("name", "reference", "email"),
    "contacts": ("first_name", "last_name", "email"),
    "staffs": ("firstname", "lastname", "email"),
    "items": ("name", "reference", "description"),
}


async def company_name_variants(client, result, query, types, archived, limit):
    words = re.findall(r"\w+", normalized(query))
    if result["data"] or not result["coverage"].get("complete") or len(words) < 2:
        return result
    # Broaden discovery, then require every original word to match the candidate name.
    # This tolerates Pharmacies / Pharmaciens without dropping an unrelated product criterion.
    incomplete = None
    for term in list(dict.fromkeys(word for word in words if len(word) >= 4))[:2]:
        broad = await client.native_search(term, types, limit=limit, archived=archived)

        def close_name(row):
            candidate_words = re.findall(r"\w+", normalized(row.get("name")))
            return all(
                any(
                    word == candidate
                    or (
                        len(word) >= 4
                        and len(candidate) >= 4
                        and SequenceMatcher(None, word, candidate).ratio() >= 0.82
                    )
                    for candidate in candidate_words
                )
                for word in words
            )

        matches = [row for row in broad["data"] if close_name(row)]
        if matches:
            broad.update(
                data=matches,
                returned=len(matches),
                match={"kind": "approximate_name", "original_query": query, "search_query": term},
            )
            return broad
        if not broad["coverage"].get("complete") or broad["coverage"].get("truncated"):
            incomplete = {
                **broad,
                "data": [],
                "returned": 0,
                "status": "incomplete",
                "match": {"kind": "approximate_name", "original_query": query, "search_query": term},
            }
    return incomplete or result


async def search_records(
    client: Any,
    path: str,
    *,
    needle: str | None = None,
    filters: dict[str, Any] | None = None,
    fields: tuple[str, ...] = (),
    limit: int = 20,
    max_pages: int | None = None,
    cursor: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    family = path.strip("/").split("/")[0]
    fallback = None
    native_filters = set(filters or {}) <= {"type", "is_archived"}
    if (
        needle
        and not cursor
        and path == f"/{family}/search"
        and family in NATIVE_TYPES
        and native_filters
        and not kwargs
    ):
        types = NATIVE_TYPES[family]
        if filters and filters.get("type") and family in {"companies", "items"}:
            root = "company" if family == "companies" else "item"
            types = [f"{root}.{kind}" for kind in filters["type"]]
        try:
            result = await client.native_search(
                needle, types, limit=limit, archived=(filters or {}).get("is_archived")
            )
            if family == "companies":
                result = await company_name_variants(
                    client, result, needle, types, (filters or {}).get("is_archived"), limit
                )
            # Catalogue search documents omit pricing; hydrate only the requested candidates.
            if family == "items":
                result["data"] = [await client.get(f"/items/{row['id']}") for row in result["data"]]
            return result
        except ToolError as exc:
            if exc.code != "permission_denied":
                raise
            fallback = {
                "strategy": "paginated_scan",
                "reason": "missing_search_scope",
                "required_scope": "search.read",
                "next_action": "Reconnecter l'application.",
            }
    result = await client.bounded_search(
        path,
        filters=filters,
        needle=needle,
        fields=fields,
        limit=limit,
        max_pages=max_pages,
        cursor=cursor,
        **kwargs,
    )
    if fallback:
        result["fallback"] = fallback
    return result


async def resolve_entity(client: Any, family: str, query: str, *, company_id: int | None = None):
    filters = {"companies": [company_id]} if family == "contacts" and company_id else {}
    result = await search_records(
        client, f"/{family}/search", needle=query, filters=filters, fields=ENTITY_FIELDS[family], limit=100
    )
    require_complete(result, f"identification {family}")
    candidates = result["data"]
    wanted = normalized(query)

    def exact(row):
        names = [row.get("name"), row.get("reference"), row.get("email")]
        first = row.get("first_name", row.get("firstname", ""))
        last = row.get("last_name", row.get("lastname", ""))
        names.extend([f"{first} {last}", f"{last} {first}"])
        return wanted in [normalized(name) for name in names if name]

    matches = [row for row in candidates if exact(row)] or candidates
    if len(matches) != 1:
        raise ToolError(
            "ambiguous" if matches else "not_found",
            "Plusieurs correspondances." if matches else "Aucune correspondance dans la recherche.",
            candidates=[
                {k: row.get(k) for k in ("id", "name", "first_name", "last_name", "email")}
                for row in matches[:10]
            ],
            coverage=result["coverage"],
            next_action="Choisir un candidat ou préciser le nom.",
        )
    return matches[0]


async def _resolve_company(client: Any, name: str):
    return await resolve_entity(client, "companies", name)


async def _resolve_staff(client: Any, query: str):
    return await resolve_entity(client, "staffs", query)


async def _resolve_contact(client: Any, query: str, *, company_id: int | None = None):
    return await resolve_entity(client, "contacts", query, company_id=company_id)


async def _resolve_company_from_query(client: Any, query: str):
    # Only remove conversational prefixes. Never guess the customer from arbitrary product tokens.
    words = normalized(query).split()
    prefixes = {
        "donne-moi",
        "donne",
        "moi",
        "trouve",
        "retrouve",
        "le",
        "les",
        "un",
        "une",
        "devis",
        "facture",
        "factures",
        "fait",
        "faite",
        "pour",
        "client",
        "societe",
        "chez",
    }
    while words and words[0] in prefixes:
        words.pop(0)
    candidate = " ".join(words) or query
    return await _resolve_company(client, candidate), candidate


async def company_id_from(client: Any, company_id: int | None, company_name: str | None):
    if company_name:
        company, _ = await _resolve_company_from_query(client, company_name)
        resolved = int(company["id"])
        if company_id is not None and company_id != resolved:
            raise ToolError("identity_conflict", "L'ID et le nom désignent des clients différents.")
        return resolved
    return company_id


def matches_product(row: dict[str, Any], text: str | None, references: list[str], ids: list[int]) -> bool:
    related = row.get("related") or {}
    if isinstance(related, dict) and related.get("id") in ids:
        return True
    if normalized(row.get("reference")) in {normalized(ref) for ref in references}:
        return True
    return bool(
        text
        and text_matches(text, " ".join(str(row.get(k, "")) for k in ("reference", "description", "name")))
    )
