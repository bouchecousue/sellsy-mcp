"""Versioned compact records returned by searches; full details remain available by ID."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class RecordSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    type: str | None = None
    object: dict[str, Any] | None = None
    name: str | None = None
    reference: str | None = None
    is_archived: bool | None = None
    related: Any = None
    created: str | None = None


class CompanySummary(RecordSummary):
    email: str | None = None
    status: str | None = None


class PersonSummary(RecordSummary):
    first_name: str | None = None
    last_name: str | None = None
    firstname: str | None = None
    lastname: str | None = None
    email: str | None = None
    company_id: int | None = None


class ItemSummary(RecordSummary):
    reference_price: Any = None
    reference_price_taxes_exc: Any = None
    reference_price_taxes_inc: Any = None
    currency: str | None = None
    unit_amount: Any = None
    purchase_amount: Any = None


class DocumentSummary(RecordSummary):
    number: str | None = None
    subject: str | None = None
    status: str | None = None
    date: str | None = None
    due_date: str | None = None
    amounts: Any = None
    currency: str | None = None
    owner: Any = None
    is_deposit: bool | None = None
    is_progress_invoice: bool | None = None
    contact_id: int | None = None


class InstallmentDate(BaseModel):
    id: int
    date: str


class SubscriptionSummary(DocumentSummary):
    next_payment_installment_date: InstallmentDate | str | None = None
    first_payment_installment_date: InstallmentDate | str | None = None
    invoices_total_count: int | None = None
    invoices_generated_count: int | None = None
    invoices_remaining_count: int | None = None
    model: Any = None
    conditions: Any = None
    order_reference: str | None = None
    start_date: str | None = None
    end_date: str | None = None


SUMMARY_MODELS = {
    "companies": CompanySummary,
    "contacts": PersonSummary,
    "staffs": PersonSummary,
    "items": ItemSummary,
    "subscriptions": SubscriptionSummary,
    **dict.fromkeys(("estimates", "invoices", "credit-notes", "orders", "deliveries"), DocumentSummary),
}
