from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .dates import in_period
from .errors import error_result, partial_results
from .models import (
    CommentSearch,
    CommercialDocumentInput,
    CompanySearch,
    ContactSearch,
    CustomerContextInput,
    EstimateSearch,
    IdInput,
    InvoiceSearch,
    ItemSearch,
    OpportunitySearch,
    RelatedFilesInput,
    RelatedPaymentsInput,
    SubscriptionSearch,
    WorkspaceSearch,
)
from .presentation import _margin_analysis, _summary, _without_costs
from .resolution import (
    _resolve_company_from_query,
    _resolve_contact,
    _resolve_staff,
    company_id_from,
    search_records,
)
from .runtime import READ_HINTS, Runtime
from .sellsy_client import SellsyClient
from .write_tools import register_estimate_tools


def _file_path(object_type: str, object_id: int) -> str:
    paths = {
        "estimate": f"/estimates/{object_id}/files",
        "opportunity": f"/opportunities/{object_id}/files",
        "invoice": f"/invoices/{object_id}/files",
        "deposit_invoice": f"/deposit-invoices/{object_id}/files",
        "company": f"/companies/{object_id}/files",
    }
    return paths[object_type]


def _payment_path(document_type: str, document_id: int) -> str:
    paths = {
        "estimate": f"/estimates/{document_id}/payments",
        "invoice": f"/invoices/{document_id}/payments",
        "deposit_invoice": f"/deposit-invoices/{document_id}/payments",
    }
    return paths[document_type]


def _document_path(document_type: str, document_id: int) -> str:
    prefix = {
        "estimate": "estimates",
        "invoice": "invoices",
        "deposit_invoice": "deposit-invoices",
    }[document_type]
    return f"/{prefix}/{document_id}"


def _document_embeds(document_type: str) -> list[str]:
    embeds = ["company", "contact"]
    if document_type != "estimate":
        embeds.append("deposits")
    return embeds


def _comment_matches(
    row: dict[str, Any],
    *,
    related_type: str | None = None,
    related_id: int | None = None,
    company_id: int | None = None,
    created_start: str | None = None,
    created_end: str | None = None,
) -> bool:
    related = row.get("related") or []
    if related_type and related_id:
        if not any(
            isinstance(item, dict)
            and item.get("type") == related_type
            and str(item.get("id")) == str(related_id)
            for item in related
        ):
            return False
    if company_id:
        company_matches = str(row.get("company_id")) == str(company_id) or any(
            isinstance(item, dict)
            and item.get("type") == "company"
            and str(item.get("id")) == str(company_id)
            for item in related
        )
        if not company_matches:
            return False
    return in_period(row.get("created"), created_start, created_end)


async def _search_comments_live(client: SellsyClient, params: CommentSearch) -> dict[str, Any]:
    """Search comments without Sellsy's currently rejected relational filters.

    Sellsy production returns HTTP 400 for documented `related_objects` filters on
    `/comments/search`. We therefore scan a bounded result set and match the
    relation fields returned on every comment.
    """
    has_local_filters = any(
        (
            params.related_type,
            params.related_id,
            params.company_id,
            params.created_start,
            params.created_end,
        )
    )
    return await search_records(
        client,
        "/comments/search",
        filters={},
        needle=params.text,
        fields=("description",),
        matcher=(
            lambda row: (
                _comment_matches(
                    row,
                    related_type=params.related_type,
                    related_id=params.related_id,
                    company_id=params.company_id,
                    created_start=params.created_start,
                    created_end=params.created_end,
                )
                if has_local_filters
                else None
            )
        ),
        matcher_key=params.model_dump(exclude={"limit", "max_pages", "cursor"})
        if has_local_filters
        else None,
        limit=params.limit,
        max_pages=params.max_pages,
        cursor=params.cursor,
    )


def register_tools(mcp: FastMCP, runtime: Runtime) -> None:
    @runtime.tool(
        mcp,
        name="sellsy_search_estimates",
        title="Rechercher des devis Sellsy",
        annotations=READ_HINTS,
        structured_output=True,
    )
    async def search_estimates(params: EstimateSearch) -> dict[str, Any]:
        """Recherche des devis par numéro, texte, société, statut ou période.

        La recherche textuelle est bornée et retourne toujours sa couverture.
        Les coûts d'achat ne sont pas exposés.
        """
        _, client = runtime.context()
        company_id = await company_id_from(client, params.company_id, params.company_name)
        owner_id = params.owner_id
        owner = None
        if params.owner_name:
            owner = await _resolve_staff(client, params.owner_name)
            if owner_id is not None and owner_id != int(owner["id"]):
                raise ValueError("owner_id et owner_name désignent des personnes différentes")
            owner_id = int(owner["id"])
        assigned_staff_id = params.assigned_staff_id
        assigned_staff = None
        if params.assigned_staff_name:
            assigned_staff = await _resolve_staff(client, params.assigned_staff_name)
            if assigned_staff_id is not None and assigned_staff_id != int(assigned_staff["id"]):
                raise ValueError("assigned_staff_id et assigned_staff_name sont contradictoires")
            assigned_staff_id = int(assigned_staff["id"])
        contact_id = params.contact_id
        contact = None
        if params.contact_name:
            contact = await _resolve_contact(client, params.contact_name, company_id=company_id)
            if contact_id is not None and contact_id != int(contact["id"]):
                raise ValueError("contact_id et contact_name désignent des contacts différents")
            contact_id = int(contact["id"])
        filters: dict[str, Any] = {}
        if params.number:
            filters["number"] = params.number
        if params.status:
            filters["status"] = list(params.status)
        if params.date_start or params.date_end:
            filters["date"] = {
                key: value for key, value in (("start", params.date_start), ("end", params.date_end)) if value
            }
        if company_id:
            filters["related_objects"] = [{"type": "company", "id": company_id}]
        if owner_id:
            filters["owners"] = [owner_id]
        if assigned_staff_id:
            filters["assigned_staff_ids"] = [assigned_staff_id]
        if contact_id:
            filters["contacts"] = [contact_id]
        result = await search_records(
            client,
            "/estimates/search",
            filters=filters,
            needle=params.text,
            fields=("number", "subject", "name"),
            limit=params.limit,
            max_pages=params.max_pages,
            cursor=params.cursor,
        )
        if not result["data"] and params.text and not company_id:
            try:
                company, matched_company_query = await _resolve_company_from_query(client, params.text)
            except ValueError as exc:
                result["company_resolution"] = error_result(exc)
                company = None
            if company:
                company_id = int(company["id"])
                fallback_filters = {
                    **filters,
                    "related_objects": [{"type": "company", "id": company_id}],
                }
                result = await search_records(
                    client,
                    "/estimates/search",
                    filters=fallback_filters,
                    limit=params.limit,
                    max_pages=params.max_pages,
                    cursor=params.cursor,
                )
                result["fallback"] = {
                    "strategy": "resolved_text_as_company",
                    "company": {"id": company_id, "name": company.get("name")},
                    "matched_query": matched_company_query,
                }
        result["data"] = [_summary(row, family="estimates") for row in result["data"]]
        if company_id:
            result["resolved_company_id"] = company_id
        if owner_id:
            result["resolved_owner"] = {
                "id": owner_id,
                "name": (
                    f"{owner.get('firstname', '')} {owner.get('lastname', '')}".strip() if owner else None
                ),
            }
        if assigned_staff_id:
            result["resolved_assigned_staff"] = {
                "id": assigned_staff_id,
                "name": (
                    f"{assigned_staff.get('firstname', '')} {assigned_staff.get('lastname', '')}".strip()
                    if assigned_staff
                    else None
                ),
            }
        if contact_id:
            result["resolved_contact"] = {
                "id": contact_id,
                "name": (
                    contact.get("name")
                    or f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip()
                    if contact
                    else None
                ),
            }
        return result

    @runtime.tool(mcp, name="sellsy_get_estimate", title="Consulter un devis Sellsy", annotations=READ_HINTS)
    async def get_estimate(params: IdInput) -> dict[str, Any]:
        """Retourne le devis live, ses lignes, montants, taxes et relations utiles."""
        _, client = runtime.context()
        result = await client.get(f"/estimates/{params.id}", params={"embed[]": ["company", "contact"]})
        return _without_costs(result, params.include_costs)

    @runtime.tool(
        mcp, name="sellsy_search_companies", title="Rechercher des sociétés Sellsy", annotations=READ_HINTS
    )
    async def search_companies(params: CompanySearch) -> dict[str, Any]:
        """Recherche native Sellsy par nom ou référence, puis parcours paginé si search.read manque.
        Ne pas conclure à une absence si la couverture est incomplète ; reprendre next_cursor."""
        _, client = runtime.context()
        filters: dict[str, Any] = {}
        if params.company_type:
            filters["type"] = params.company_type
        if params.is_archived is not None:
            filters["is_archived"] = params.is_archived
        result = await search_records(
            client,
            "/companies/search",
            filters=filters,
            needle=params.text,
            fields=("name", "reference", "email"),
            limit=params.limit,
            max_pages=params.max_pages,
            cursor=params.cursor,
        )
        result["data"] = [_summary(row, family="companies") for row in result["data"]]
        return result

    @runtime.tool(
        mcp, name="sellsy_get_company", title="Consulter une société Sellsy", annotations=READ_HINTS
    )
    async def get_company(params: IdInput) -> dict[str, Any]:
        """Retourne la société live et ses contacts liés."""
        _, client = runtime.context()
        company = await client.get(f"/companies/{params.id}")
        contacts = await client.list_all(f"/companies/{params.id}/contacts", limit=100)
        return {"company": company, "contacts": contacts["data"], "contacts_search": contacts}

    @runtime.tool(
        mcp, name="sellsy_search_contacts", title="Rechercher des contacts Sellsy", annotations=READ_HINTS
    )
    async def search_contacts(params: ContactSearch) -> dict[str, Any]:
        """Recherche des contacts par nom ou e-mail, globalement ou au sein d'une société."""
        _, client = runtime.context()
        filters: dict[str, Any] = {}
        company_id = await company_id_from(client, params.company_id, params.company_name)
        if company_id:
            filters["companies"] = [company_id]
        result = await search_records(
            client,
            "/contacts/search",
            filters=filters,
            needle=params.text,
            fields=("name", "first_name", "last_name", "email"),
            limit=params.limit,
            max_pages=params.max_pages,
            cursor=params.cursor,
        )
        result["data"] = [_summary(row, family="contacts") for row in result["data"]]
        if company_id:
            result["resolved_company_id"] = company_id
        return result

    @runtime.tool(mcp, name="sellsy_get_contact", title="Consulter un contact Sellsy", annotations=READ_HINTS)
    async def get_contact(params: IdInput) -> dict[str, Any]:
        """Retourne le contact live."""
        _, client = runtime.context()
        return await client.get(f"/contacts/{params.id}")

    @runtime.tool(
        mcp, name="sellsy_search_items", title="Rechercher des articles Sellsy", annotations=READ_HINTS
    )
    async def search_items(params: ItemSearch) -> dict[str, Any]:
        """Recherche le catalogue par référence exacte ou texte borné. Les coûts sont opt-in."""
        _, client = runtime.context()
        filters: dict[str, Any] = {}
        if params.reference:
            filters["reference"] = params.reference
        if params.item_type:
            filters["type"] = params.item_type
        result = await search_records(
            client,
            "/items/search",
            filters=filters,
            needle=params.text,
            fields=("name", "reference", "description"),
            limit=params.limit,
            max_pages=params.max_pages,
            cursor=params.cursor,
        )
        result["data"] = [
            _summary(row, include_costs=params.include_costs, family="items") for row in result["data"]
        ]
        return result

    @runtime.tool(mcp, name="sellsy_get_item", title="Consulter un article Sellsy", annotations=READ_HINTS)
    async def get_item(params: IdInput) -> dict[str, Any]:
        """Retourne l'article live. purchase_amount est masqué sauf demande explicite."""
        _, client = runtime.context()
        return _without_costs(await client.get(f"/items/{params.id}"), params.include_costs)

    @runtime.tool(
        mcp,
        name="sellsy_search_subscriptions",
        title="Rechercher des abonnements Sellsy",
        annotations=READ_HINTS,
    )
    async def search_subscriptions(params: SubscriptionSearch) -> dict[str, Any]:
        """Recherche les abonnements, notamment par société, puis filtre le texte de façon bornée."""
        _, client = runtime.context()
        company_id = await company_id_from(client, params.company_id, params.company_name)
        filters: dict[str, Any] = {}
        if company_id:
            filters["related_objects"] = [{"type": "company", "id": company_id}]
        result = await search_records(
            client,
            "/subscriptions/search",
            filters=filters,
            needle=params.text,
            fields=("number", "model", "order_reference", "conditions"),
            limit=params.limit,
            max_pages=params.max_pages,
            cursor=params.cursor,
        )
        result["data"] = [_summary(row, family="subscriptions") for row in result["data"]]
        if company_id:
            result["resolved_company_id"] = company_id
        return result

    @runtime.tool(
        mcp, name="sellsy_get_subscription", title="Consulter un abonnement Sellsy", annotations=READ_HINTS
    )
    async def get_subscription(params: IdInput) -> dict[str, Any]:
        """Retourne le détail live d'un abonnement Sellsy."""
        _, client = runtime.context()
        return _without_costs(await client.get(f"/subscriptions/{params.id}"), params.include_costs)

    @runtime.tool(
        mcp,
        name="sellsy_search_opportunities",
        title="Rechercher des opportunités Sellsy",
        annotations=READ_HINTS,
    )
    async def search_opportunities(params: OpportunitySearch) -> dict[str, Any]:
        """Recherche les opportunités par nom, client, statut, pipeline, étape, responsable ou échéance."""
        _, client = runtime.context()
        company_id = await company_id_from(client, params.company_id, params.company_name)
        filters: dict[str, Any] = {}
        if company_id:
            filters["related_objects"] = [{"type": "company", "id": company_id}]
        if params.status:
            filters["statuses"] = list(params.status)
        if params.pipeline_ids:
            filters["pipeline"] = params.pipeline_ids
        if params.step_ids:
            filters["step"] = params.step_ids
        if params.owner_ids:
            filters["owners"] = params.owner_ids
        if params.due_date_start or params.due_date_end:
            filters["due_date"] = {
                key: value
                for key, value in (
                    ("start", params.due_date_start),
                    ("end", params.due_date_end),
                )
                if value
            }
        result = await search_records(
            client,
            "/opportunities/search",
            filters=filters,
            needle=params.text,
            fields=("number", "name", "note"),
            limit=params.limit,
            max_pages=params.max_pages,
            cursor=params.cursor,
        )
        if not result["data"] and params.text and not company_id:
            try:
                company, matched_company_query = await _resolve_company_from_query(client, params.text)
            except ValueError as exc:
                result["company_resolution"] = error_result(exc)
                company = None
            if company:
                company_id = int(company["id"])
                filters["related_objects"] = [{"type": "company", "id": company_id}]
                result = await search_records(
                    client,
                    "/opportunities/search",
                    filters=filters,
                    limit=params.limit,
                    max_pages=params.max_pages,
                    cursor=params.cursor,
                )
                result["fallback"] = {
                    "strategy": "resolved_text_as_company",
                    "company": {"id": company_id, "name": company.get("name")},
                    "matched_query": matched_company_query,
                }
        result["data"] = [_summary(row) for row in result["data"]]
        if company_id:
            result["resolved_company_id"] = company_id
        return result

    @runtime.tool(
        mcp,
        name="sellsy_get_opportunity",
        title="Consulter une opportunité Sellsy",
        annotations=READ_HINTS,
    )
    async def get_opportunity(params: IdInput) -> dict[str, Any]:
        """Retourne l'opportunité live, sa note, son client, ses contacts et ses relations."""
        _, client = runtime.context()
        return await client.get(f"/opportunities/{params.id}", params={"embed[]": ["company", "contacts"]})

    async def _search_invoices(
        client: SellsyClient, params: InvoiceSearch, *, deposits: bool
    ) -> dict[str, Any]:
        company_id = await company_id_from(client, params.company_id, params.company_name)
        owner_id = params.owner_id
        owner = None
        if params.owner_name:
            owner = await _resolve_staff(client, params.owner_name)
            if owner_id is not None and owner_id != int(owner["id"]):
                raise ValueError("owner_id et owner_name désignent des personnes différentes")
            owner_id = int(owner["id"])
        assigned_staff_id = params.assigned_staff_id
        assigned_staff = None
        if params.assigned_staff_name:
            assigned_staff = await _resolve_staff(client, params.assigned_staff_name)
            if assigned_staff_id is not None and assigned_staff_id != int(assigned_staff["id"]):
                raise ValueError("assigned_staff_id et assigned_staff_name sont contradictoires")
            assigned_staff_id = int(assigned_staff["id"])
        contact_id = params.contact_id
        contact = None
        if params.contact_name:
            contact = await _resolve_contact(client, params.contact_name, company_id=company_id)
            if contact_id is not None and contact_id != int(contact["id"]):
                raise ValueError("contact_id et contact_name désignent des contacts différents")
            contact_id = int(contact["id"])
        filters: dict[str, Any] = {}
        if params.number:
            filters["number"] = params.number
        if params.status:
            filters["status"] = list(params.status)
        if params.date_start or params.date_end:
            filters["date"] = {
                key: value for key, value in (("start", params.date_start), ("end", params.date_end)) if value
            }
        if company_id:
            filters["related_objects"] = [{"type": "company", "id": company_id}]
        if owner_id:
            filters["owners"] = [owner_id]
        if assigned_staff_id:
            filters["assigned_staff_ids"] = [assigned_staff_id]
        if contact_id:
            filters["contacts"] = [contact_id]
        for key in ("is_deposit", "is_progress_invoice"):
            if getattr(params, key) is not None:
                filters[key] = getattr(params, key)
        path = "/deposit-invoices/search" if deposits else "/invoices/search"
        result = await search_records(
            client,
            path,
            filters=filters,
            needle=params.text,
            fields=("number", "subject"),
            limit=params.limit,
            max_pages=params.max_pages,
            cursor=params.cursor,
        )
        if not result["data"] and params.text and not company_id:
            try:
                company, matched_company_query = await _resolve_company_from_query(client, params.text)
            except ValueError as exc:
                result["company_resolution"] = error_result(exc)
                company = None
            if company:
                company_id = int(company["id"])
                filters["related_objects"] = [{"type": "company", "id": company_id}]
                result = await search_records(
                    client,
                    path,
                    filters=filters,
                    limit=params.limit,
                    max_pages=params.max_pages,
                    cursor=params.cursor,
                )
                result["fallback"] = {
                    "strategy": "resolved_text_as_company",
                    "company": {"id": company_id, "name": company.get("name")},
                    "matched_query": matched_company_query,
                }
        result["data"] = [_summary(row, family="invoices") for row in result["data"]]
        if company_id:
            result["resolved_company_id"] = company_id
        if owner_id:
            result["resolved_owner"] = {
                "id": owner_id,
                "name": (
                    f"{owner.get('firstname', '')} {owner.get('lastname', '')}".strip() if owner else None
                ),
            }
        if assigned_staff_id:
            result["resolved_assigned_staff"] = {
                "id": assigned_staff_id,
                "name": (
                    f"{assigned_staff.get('firstname', '')} {assigned_staff.get('lastname', '')}".strip()
                    if assigned_staff
                    else None
                ),
            }
        if contact_id:
            result["resolved_contact"] = {
                "id": contact_id,
                "name": (
                    contact.get("name")
                    or f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip()
                    if contact
                    else None
                ),
            }
        return result

    @runtime.tool(
        mcp,
        name="sellsy_search_invoices",
        title="Rechercher des factures Sellsy",
        annotations=READ_HINTS,
    )
    async def search_invoices(params: InvoiceSearch) -> dict[str, Any]:
        """Recherche des factures par numéro, texte, client, statut ou période."""
        _, client = runtime.context()
        return await _search_invoices(client, params, deposits=False)

    @runtime.tool(
        mcp, name="sellsy_get_invoice", title="Consulter une facture Sellsy", annotations=READ_HINTS
    )
    async def get_invoice(params: IdInput) -> dict[str, Any]:
        """Retourne la facture live, ses lignes, échéance, reste dû, client et acomptes liés."""
        _, client = runtime.context()
        result = await client.get(
            f"/invoices/{params.id}",
            params={"embed[]": ["company", "contact", "deposits"]},
        )
        return _without_costs(result, params.include_costs)

    @runtime.tool(
        mcp,
        name="sellsy_search_deposit_invoices",
        title="Rechercher des factures d'acompte Sellsy",
        annotations=READ_HINTS,
    )
    async def search_deposit_invoices(params: InvoiceSearch) -> dict[str, Any]:
        """Recherche les factures d'acompte par numéro, texte, client, statut ou période."""
        _, client = runtime.context()
        return await _search_invoices(client, params, deposits=True)

    @runtime.tool(
        mcp,
        name="sellsy_get_deposit_invoice",
        title="Consulter une facture d'acompte Sellsy",
        annotations=READ_HINTS,
    )
    async def get_deposit_invoice(params: IdInput) -> dict[str, Any]:
        """Retourne l'acompte live, ses lignes, échéance, reste dû et client."""
        _, client = runtime.context()
        result = await client.get(
            f"/deposit-invoices/{params.id}",
            params={"embed[]": ["company", "contact", "deposits"]},
        )
        return _without_costs(result, params.include_costs)

    @runtime.tool(
        mcp,
        name="sellsy_search_comments",
        title="Rechercher et lire des commentaires Sellsy",
        annotations=READ_HINTS,
    )
    async def search_comments(params: CommentSearch) -> dict[str, Any]:
        """Recherche les commentaires, globalement ou pour un objet commercial ou un client."""
        _, client = runtime.context()
        return await _search_comments_live(client, params)

    @runtime.tool(
        mcp,
        name="sellsy_list_related_files",
        title="Lister les fichiers liés dans Sellsy",
        annotations=READ_HINTS,
    )
    async def list_related_files(params: RelatedFilesInput) -> dict[str, Any]:
        """Liste les fichiers liés à un devis, une opportunité, une facture, un acompte ou une société."""
        _, client = runtime.context()
        return await client.list_all(
            _file_path(params.object_type, params.object_id),
            limit=params.limit,
            max_pages=params.max_pages,
            cursor=params.cursor,
        )

    @runtime.tool(
        mcp,
        name="sellsy_list_related_payments",
        title="Lister les paiements liés dans Sellsy",
        annotations=READ_HINTS,
    )
    async def list_related_payments(params: RelatedPaymentsInput) -> dict[str, Any]:
        """Liste les paiements liés à un devis, une facture ou une facture d'acompte."""
        _, client = runtime.context()
        return await client.list_all(
            _payment_path(params.document_type, params.document_id),
            limit=params.limit,
            max_pages=params.max_pages,
            cursor=params.cursor,
        )

    @runtime.tool(
        mcp,
        name="sellsy_analyze_commercial_document",
        title="Analyser un devis ou une facture Sellsy",
        annotations=READ_HINTS,
    )
    async def analyze_commercial_document(params: CommercialDocumentInput) -> dict[str, Any]:
        """Regroupe document, client, commentaires, fichiers, paiements et marge estimée optionnelle."""
        _, client = runtime.context()
        detail_path = _document_path(params.document_type, params.document_id)
        comments_type = "estimate" if params.document_type == "estimate" else "invoice"
        document = await client.get(detail_path, params={"embed[]": _document_embeds(params.document_type)})
        parts = await partial_results(
            comments=_search_comments_live(
                client,
                CommentSearch(
                    related_type=comments_type, related_id=params.document_id, limit=100, max_pages=10
                ),
            ),
            files=client.list_all(_file_path(params.document_type, params.document_id)),
            payments=client.list_all(_payment_path(params.document_type, params.document_id)),
        )
        result: dict[str, Any] = {
            "document": _without_costs(document, params.include_costs),
            **parts,
            "partial": any(
                "error" in value or value.get("coverage", {}).get("truncated") for value in parts.values()
            ),
        }
        if params.include_costs:
            result["estimated_margin"] = _margin_analysis(document)
        else:
            result["costs"] = {
                "included": False,
                "hint": "Set include_costs=true to expose purchase prices and estimated margin.",
            }
        return result

    @runtime.tool(
        mcp,
        name="sellsy_get_customer_context",
        title="Ouvrir un aperçu du dossier client Sellsy",
        annotations=READ_HINTS,
    )
    async def get_customer_context(params: CustomerContextInput) -> dict[str, Any]:
        """Regroupe un client, ses contacts, devis, opportunités, factures, acomptes et abonnements."""
        _, client = runtime.context()
        company_id = await company_id_from(client, params.company_id, params.company_name)
        company = await client.get(f"/companies/{company_id}")
        related = {"related_objects": [{"type": "company", "id": company_id}]}
        parts = await partial_results(
            contacts=client.list_all(f"/companies/{company_id}/contacts", limit=100),
            **{
                family: client.bounded_search(
                    f"/{family.replace('_', '-')}/search", filters=related, limit=params.limit_per_family
                )
                for family in ("estimates", "opportunities", "invoices", "deposit_invoices", "subscriptions")
            },
        )
        for family, part in parts.items():
            if "data" in part:
                part["data"] = [_summary(row, family=family) for row in part["data"]]
        return {
            "company": company,
            **parts,
            "view": "overview",
            "partial": any(
                "error" in value or value.get("coverage", {}).get("truncated") for value in parts.values()
            ),
        }

    @runtime.tool(
        mcp,
        name="sellsy_search_workspace",
        title="Rechercher partout dans Sellsy",
        annotations=READ_HINTS,
    )
    async def search_workspace(params: WorkspaceSearch) -> dict[str, Any]:
        """Recherche un texte dans les clients, contacts, documents, opportunités, articles et abonnements."""
        _, client = runtime.context()
        families = params.object_types or [
            "companies",
            "contacts",
            "estimates",
            "opportunities",
            "invoices",
            "deposit_invoices",
            "items",
            "subscriptions",
        ]
        resolution_error = None
        try:
            resolved_company, matched_company_query = await _resolve_company_from_query(client, params.text)
        except ValueError as exc:
            resolution_error = error_result(exc)
            resolved_company = None
            matched_company_query = None
        relation = (
            {"related_objects": [{"type": "company", "id": int(resolved_company["id"])}]}
            if resolved_company
            else {}
        )
        specs = {
            "companies": ("/companies/search", {}, ("name", "reference", "email")),
            "contacts": (
                "/contacts/search",
                {},
                ("name", "first_name", "last_name", "email"),
            ),
            "estimates": ("/estimates/search", relation, ("number", "subject", "name")),
            "opportunities": (
                "/opportunities/search",
                relation,
                ("number", "name", "note"),
            ),
            "invoices": ("/invoices/search", relation, ("number", "subject")),
            "deposit_invoices": (
                "/deposit-invoices/search",
                relation,
                ("number", "subject"),
            ),
            "items": ("/items/search", {}, ("name", "reference", "description")),
            "subscriptions": (
                "/subscriptions/search",
                relation,
                ("number", "name", "subject", "status"),
            ),
        }

        async def one(family: str) -> dict[str, Any]:
            path, filters, fields = specs[family]
            result = await search_records(
                client,
                path,
                filters=filters,
                needle=None if filters else params.text,
                fields=fields,
                limit=params.limit_per_family,
                max_pages=params.max_pages,
            )
            result["data"] = [_summary(row) for row in result["data"]]
            return result

        results = await partial_results(**{family: one(family) for family in families})
        return {
            "query": params.text,
            "resolved_company": (
                {
                    "id": resolved_company.get("id"),
                    "name": resolved_company.get("name"),
                    "matched_query": matched_company_query,
                }
                if resolved_company
                else None
            ),
            "results": results,
            "company_resolution": resolution_error or {"status": "resolved"},
            "partial": any("error" in v or v.get("coverage", {}).get("truncated") for v in results.values()),
        }

    from .extra_tools import register_extra_tools

    register_estimate_tools(mcp, runtime)
    register_extra_tools(mcp, runtime)
