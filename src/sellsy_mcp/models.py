from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .dates import boundary

EstimateStatus = Literal[
    "draft",
    "sent",
    "read",
    "accepted",
    "refused",
    "expired",
    "advanced",
    "partialinvoiced",
    "invoiced",
    "cancelled",
]
OpportunityStatus = Literal["open", "won", "lost", "cancelled", "closed", "late"]
InvoiceStatus = Literal["draft", "due", "payinprogress", "paid", "late", "cancelled"]
CommentRelatedType = Literal[
    "opportunity",
    "company",
    "individual",
    "contact",
    "item",
    "estimate",
    "creditnote",
    "order",
    "delivery",
    "proforma",
    "invoice",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="after")
    def validate_periods(self):
        for prefix in ("date", "due_date", "created", "updated"):
            start, end = getattr(self, prefix + "_start", None), getattr(self, prefix + "_end", None)
            if start:
                boundary(start)
            if end:
                boundary(end, end=True)
            if start and end and boundary(start) > boundary(end, end=True):
                raise ValueError("La date de début doit précéder la date de fin.")
        for name in ("text", "company_name", "owner_name", "assigned_staff_name", "contact_name"):
            value = getattr(self, name, None)
            if value is not None and not value.strip():
                raise ValueError(f"{name} ne doit pas être vide")
        return self


class EstimateSearch(StrictModel):
    number: str | None = Field(None, description="Numéro exact du devis.")
    text: str | None = Field(None, description="Texte contenu dans le numéro, sujet ou libellé client.")
    company_id: int | None = Field(None, gt=0)
    company_name: str | None = None
    owner_id: int | None = Field(None, gt=0, description="ID de l'auteur/propriétaire Sellsy.")
    owner_name: str | None = Field(None, description="Nom ou e-mail de l'auteur/propriétaire.")
    assigned_staff_id: int | None = Field(None, gt=0)
    assigned_staff_name: str | None = None
    contact_id: int | None = Field(None, gt=0)
    contact_name: str | None = Field(None, description="Nom ou e-mail du contact du devis.")
    status: list[EstimateStatus] | None = None
    date_start: str | None = None
    date_end: str | None = None
    limit: int = Field(20, ge=1, le=100)
    max_pages: int | None = Field(None, ge=1, le=100)
    cursor: str | None = Field(
        None, max_length=256, description="next_cursor de la réponse précédente, mêmes filtres."
    )


class IdInput(StrictModel):
    id: int = Field(gt=0)
    include_costs: bool = Field(
        False, description="Afficher purchase_amount uniquement si explicitement demandé."
    )


class TextSearch(StrictModel):
    text: str | None = None
    limit: int = Field(20, ge=1, le=100)
    max_pages: int | None = Field(None, ge=1, le=100)
    cursor: str | None = Field(
        None, max_length=256, description="next_cursor de la réponse précédente, mêmes filtres."
    )


class CompanySearch(TextSearch):
    company_type: list[Literal["client", "prospect", "supplier"]] | None = None
    is_archived: bool | None = None


class ContactSearch(TextSearch):
    company_id: int | None = Field(None, gt=0)
    company_name: str | None = None


class ItemSearch(TextSearch):
    reference: str | None = None
    item_type: list[Literal["product", "service", "shipping", "packaging"]] | None = None
    include_costs: bool = False


class SubscriptionSearch(TextSearch):
    company_id: int | None = Field(None, gt=0)
    company_name: str | None = None


class OpportunitySearch(TextSearch):
    company_id: int | None = Field(None, gt=0)
    company_name: str | None = None
    status: list[OpportunityStatus] | None = None
    pipeline_ids: list[int] | None = None
    step_ids: list[int] | None = None
    owner_ids: list[int] | None = None
    due_date_start: str | None = None
    due_date_end: str | None = None


class InvoiceSearch(TextSearch):
    is_deposit: bool | None = None
    is_progress_invoice: bool | None = None
    number: str | None = Field(None, description="Numéro exact de facture.")
    company_id: int | None = Field(None, gt=0)
    company_name: str | None = None
    owner_id: int | None = Field(None, gt=0, description="ID de l'auteur/propriétaire Sellsy.")
    owner_name: str | None = Field(None, description="Nom ou e-mail de l'auteur/propriétaire.")
    assigned_staff_id: int | None = Field(None, gt=0)
    assigned_staff_name: str | None = None
    contact_id: int | None = Field(None, gt=0)
    contact_name: str | None = Field(None, description="Nom ou e-mail du contact de la facture.")
    status: list[InvoiceStatus] | None = None
    date_start: str | None = None
    date_end: str | None = None


class CommentSearch(TextSearch):
    related_type: CommentRelatedType | None = None
    related_id: int | None = Field(None, gt=0)
    company_id: int | None = Field(None, gt=0)
    created_start: str | None = None
    created_end: str | None = None

    @model_validator(mode="after")
    def related_pair_is_complete(self):
        if (self.related_type is None) != (self.related_id is None):
            raise ValueError("related_type and related_id must be provided together")
        return self


class PaginationInput(StrictModel):
    limit: int = Field(100, ge=1, le=100)
    max_pages: int | None = Field(None, ge=1, le=100)
    cursor: str | None = Field(None, max_length=256)


class CompanyContacts(PaginationInput):
    company_id: int = Field(gt=0)


class RelatedFilesInput(PaginationInput):
    object_type: Literal["estimate", "opportunity", "invoice", "deposit_invoice", "company"]
    object_id: int = Field(gt=0)


class RelatedPaymentsInput(PaginationInput):
    document_type: Literal["estimate", "invoice", "deposit_invoice"]
    document_id: int = Field(gt=0)


class CommercialDocumentInput(StrictModel):
    document_type: Literal["estimate", "invoice", "deposit_invoice"]
    document_id: int = Field(gt=0)
    include_costs: bool = Field(
        False,
        description="Inclure les prix d'achat et calculer une marge estimée ligne par ligne.",
    )


class CustomerContextInput(StrictModel):
    company_id: int | None = Field(None, gt=0)
    company_name: str | None = None
    limit_per_family: int = Field(10, ge=1, le=50)

    @model_validator(mode="after")
    def require_company(self):
        if self.company_id is None and not self.company_name:
            raise ValueError("company_id or company_name is required")
        return self


class WorkspaceSearch(StrictModel):
    text: str = Field(min_length=2)
    object_types: (
        list[
            Literal[
                "companies",
                "contacts",
                "estimates",
                "opportunities",
                "invoices",
                "deposit_invoices",
                "items",
                "subscriptions",
            ]
        ]
        | None
    ) = None
    limit_per_family: int = Field(5, ge=1, le=20)
    max_pages: int = Field(5, ge=1, le=20)


class EstimateCreateRow(StrictModel):
    type: Literal["catalog", "single"] = "catalog"
    item_id: int | None = Field(None, gt=0)
    related_type: Literal["product", "service", "packaging", "shipping"] | None = None
    reference: str | None = None
    description: str = Field(min_length=1)
    quantity: str
    unit_amount: str
    purchase_amount: str | None = None
    tax_id: int = Field(gt=0)

    @model_validator(mode="after")
    def catalog_requires_item(self):
        if self.type == "catalog" and self.item_id is None:
            raise ValueError("item_id is required for catalog rows")
        return self

    @field_validator("quantity", "unit_amount", "purchase_amount")
    @classmethod
    def validate_decimal_string(cls, value: str | None, info):
        if value is None:
            return value
        try:
            decimal = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{info.field_name} must be a decimal string") from exc
        if not decimal.is_finite() or decimal < 0:
            raise ValueError(f"{info.field_name} must be finite and non-negative")
        return value


class EstimateCreate(StrictModel):
    company_id: int = Field(gt=0)
    contact_id: int | None = Field(None, gt=0)
    invoicing_address_id: int | None = Field(None, gt=0)
    delivery_address_id: int | None = Field(None, gt=0)
    subject: str = Field(min_length=1)
    rows: list[EstimateCreateRow] = Field(min_length=1, max_length=500)
    include_costs_in_preview: bool = Field(
        False,
        description="Doit être true pour préparer des purchase_amount visibles dans l'aperçu.",
    )


class EstimateRowPatch(StrictModel):
    id: int = Field(gt=0)
    quantity: str | None = None
    unit_amount: str | None = None
    purchase_amount: str | None = None
    description: str | None = Field(None, min_length=1)
    tax_id: int | None = Field(None, gt=0)

    @model_validator(mode="after")
    def require_a_change(self):
        fields = (self.quantity, self.unit_amount, self.purchase_amount, self.description, self.tax_id)
        if all(value is None for value in fields):
            raise ValueError("At least one row field must change")
        return self

    @field_validator("quantity", "unit_amount", "purchase_amount")
    @classmethod
    def validate_decimal_string(cls, value: str | None, info):
        return EstimateCreateRow.validate_decimal_string(value, info)


class EstimateUpdate(StrictModel):
    estimate_id: int = Field(gt=0)
    rows: list[EstimateRowPatch] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_rows(self):
        if len({row.id for row in self.rows}) != len(self.rows):
            raise ValueError("Chaque ID de ligne doit être unique.")
        return self

    include_costs_in_preview: bool = Field(
        False,
        description="Doit être true pour préparer des purchase_amount visibles dans le diff.",
    )


class EstimateStatusUpdate(StrictModel):
    estimate_id: int = Field(gt=0)
    status: EstimateStatus


class OpportunityCreate(StrictModel):
    company_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=500)
    pipeline_id: int = Field(gt=0, description="Pipeline relu avec sellsy_search_records.")
    step_id: int = Field(gt=0, description="Étape appartenant au pipeline choisi.")
    contact_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list, max_length=100)
    note: str | None = Field(None, max_length=50000, description="Contexte et liens vers les sources.")
    amount: str | None = Field(None, description="Montant potentiel connu, sinon omettre ; devise du compte.")
    probability: int | None = Field(None, ge=0, le=100)
    due_date: date | None = None
    owner_id: int | None = Field(None, gt=0)
    assigned_staff_ids: list[Annotated[int, Field(gt=0)]] | None = Field(None, max_length=100)
    main_estimate_id: int | None = Field(
        None, gt=0, description="Devis du même client, encore sans opportunité."
    )

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value, info):
        return EstimateCreateRow.validate_decimal_string(value, info)

    @model_validator(mode="after")
    def unique_people(self):
        for ids in (self.contact_ids, self.assigned_staff_ids or []):
            if len(ids) != len(set(ids)):
                raise ValueError("Les IDs de contacts et collaborateurs doivent être uniques.")
        return self


class OpportunityUpdate(StrictModel):
    opportunity_id: int = Field(gt=0)
    name: str | None = Field(None, min_length=1, max_length=500)
    note: str | None = Field(
        None, max_length=50000, description="Remplace la note complète ; vide pour effacer."
    )
    amount: str | None = None
    probability: int | None = Field(None, ge=0, le=100)
    due_date: date | None = Field(None, description="Omettre pour conserver ; null pour effacer.")
    status: OpportunityStatus | None = None
    pipeline_id: int | None = Field(None, gt=0)
    step_id: int | None = Field(None, gt=0)
    owner_id: int | None = Field(None, gt=0)
    assigned_staff_ids: list[Annotated[int, Field(gt=0)]] | None = Field(None, max_length=100)
    contact_ids: list[Annotated[int, Field(gt=0)]] | None = Field(None, max_length=100)

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value, info):
        return EstimateCreateRow.validate_decimal_string(value, info)

    @model_validator(mode="after")
    def validate_changes(self):
        fields = self.model_fields_set - {"opportunity_id"}
        if not fields:
            raise ValueError("Indiquer au moins un champ à modifier.")
        for key in fields - {"due_date"}:
            if getattr(self, key) is None:
                raise ValueError(f"Omettre {key} pour le conserver ; null n'est pas autorisé.")
        if "pipeline_id" in fields and "step_id" not in fields:
            raise ValueError("Indiquer une étape lors d'un changement de pipeline.")
        for ids in (self.contact_ids or [], self.assigned_staff_ids or []):
            if len(ids) != len(set(ids)):
                raise ValueError("Les IDs doivent être uniques.")
        return self


class ApplyManifest(StrictModel):
    manifest_id: str = Field(min_length=20)
    approved: Literal[True] = Field(
        description="Doit être true après approbation explicite de l'utilisateur."
    )


class ProductHistory(StrictModel):
    company_id: int | None = Field(None, gt=0)
    company_name: str | None = None
    product_text: str | None = Field(None, min_length=2)
    references: list[str] = Field(default_factory=list, max_length=50)
    item_ids: list[int] = Field(default_factory=list, max_length=50)
    date_start: str | None = None
    date_end: str | None = None
    document_limit: int = Field(100, ge=1, le=500)
    max_pages: int = Field(50, ge=1, le=100)
    cursor: str | None = Field(None, max_length=256)

    @model_validator(mode="after")
    def require_identifiers(self):
        if not self.company_id and not self.company_name:
            raise ValueError("Indiquer le client.")
        if not self.product_text and not self.references and not self.item_ids:
            raise ValueError("Indiquer un produit, ses références ou ses IDs.")
        if any(i <= 0 for i in self.item_ids) or any(not s.strip() for s in self.references):
            raise ValueError("Références et IDs invalides.")
        return self


class NativeSearch(StrictModel):
    text: str = Field(min_length=2)
    object_types: list[
        Literal[
            "company",
            "contact",
            "item",
            "staff",
            "opportunity",
            "document.estimate",
            "document.invoice",
            "document.creditnote",
            "document.order",
            "document.delivery",
        ]
    ] = Field(min_length=1, max_length=10)
    limit: int = Field(20, ge=1, le=100)
    archived: bool | None = None


class ExtraSearch(TextSearch):
    object_type: Literal["credit_note", "order", "delivery", "staff", "pipeline", "step", "tax"]
    pipeline_id: int | None = Field(None, gt=0, description="Obligatoire pour rechercher les étapes.")
    company_id: int | None = Field(None, gt=0)
    date_start: str | None = None
    date_end: str | None = None


class ExtraDetail(StrictModel):
    object_type: Literal["credit_note", "order", "delivery", "subscription", "company", "item"]
    id: int = Field(gt=0)
    include_costs: bool = False


class LinkedData(PaginationInput):
    object_type: Literal[
        "company", "contact", "estimate", "invoice", "order", "delivery", "credit_note", "item"
    ]
    id: int = Field(gt=0)
    kind: Literal["custom_fields", "smart_tags", "relations"]


class FileRead(StrictModel):
    id: int = Field(gt=0)
    max_bytes: int = Field(1000000, ge=1000, le=5000000)


class ManifestStatus(StrictModel):
    manifest_id: str = Field(min_length=20)
