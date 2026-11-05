from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .outputs import SUMMARY_MODELS


def _without_costs(value: Any, include_costs: bool = False) -> Any:
    if include_costs:
        return value
    if isinstance(value, dict):
        return {key: _without_costs(item, False) for key, item in value.items() if key != "purchase_amount"}
    if isinstance(value, list):
        return [_without_costs(item, False) for item in value]
    return value


def _summary(
    row: dict[str, Any], *, include_costs: bool = False, family: str | None = None
) -> dict[str, Any]:
    if family in SUMMARY_MODELS:
        return _without_costs(
            SUMMARY_MODELS[family].model_validate(row).model_dump(exclude_unset=True), include_costs
        )
    keys = (
        "id",
        "object",
        "first_name",
        "last_name",
        "firstname",
        "lastname",
        "email",
        "reference_price",
        "reference_price_taxes_exc",
        "reference_price_taxes_inc",
        "next_payment_installment_date",
        "first_payment_installment_date",
        "invoices_total_count",
        "invoices_generated_count",
        "invoices_remaining_count",
        "model",
        "conditions",
        "order_reference",
        "number",
        "name",
        "subject",
        "reference",
        "status",
        "date",
        "due_date",
        "created",
        "start_date",
        "end_date",
        "amounts",
        "amount",
        "currency",
        "type",
        "pipeline",
        "step",
        "probability",
        "owner",
        "is_deposit",
        "is_progress_invoice",
        "is_archived",
        "related",
        "company_id",
        "contact_id",
        "unit_amount",
        "purchase_amount",
    )
    return _without_costs({key: row.get(key) for key in keys if key in row}, include_costs)


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _margin_analysis(document: dict[str, Any]) -> dict[str, Any]:
    purchase_total = Decimal("0")
    included = 0
    missing_cost = 0
    ignored = 0
    rows: list[dict[str, Any]] = []
    for row in document.get("rows") or []:
        if not isinstance(row, dict):
            ignored += 1
            continue
        if row.get("type") not in {None, "single", "catalog", "shipping", "packaging"}:
            ignored += 1
            continue
        quantity = _decimal(row.get("quantity"))
        unit_sale = _decimal(row.get("unit_amount"))
        unit_purchase = _decimal(row.get("purchase_amount"))
        net_sale = _decimal(row.get("amount_tax_exc"))
        approximate = net_sale is None
        if net_sale is None and quantity is not None and unit_sale is not None:
            net_sale = quantity * unit_sale
        if quantity is None or net_sale is None:
            ignored += 1
            continue
        entry: dict[str, Any] = {
            "row_id": row.get("id"),
            "sale_basis": "quantity_times_price" if approximate else "net_line_amount",
            "reference": row.get("reference"),
            "description": row.get("description"),
            "quantity": str(quantity),
            "unit_sale_excl_tax": str(unit_sale) if unit_sale is not None else None,
            "net_sale_excl_tax": str(net_sale),
        }
        if unit_purchase is None:
            missing_cost += 1
            entry["cost_coverage"] = "missing_purchase_amount"
        else:
            cost = quantity * unit_purchase
            margin = net_sale - cost
            purchase_total += cost
            included += 1
            entry.update(
                {
                    "unit_purchase_excl_tax": str(unit_purchase),
                    "purchase_total_excl_tax": str(cost),
                    "estimated_margin_excl_tax": str(margin),
                    "estimated_margin_rate_percent": (
                        str((margin / net_sale * Decimal("100")).quantize(Decimal("0.01")))
                        if net_sale != 0
                        else None
                    ),
                }
            )
        rows.append(entry)
    covered_sale = sum(
        (_decimal(row.get("net_sale_excl_tax")) or Decimal("0"))
        for row in rows
        if "purchase_total_excl_tax" in row
    )
    margin = covered_sale - purchase_total
    return {
        "basis": "estimated_by_mcp_from_document_rows",
        "formula": "net line sale excl. tax - quantity x purchase_amount",
        "discount_handling": (
            "amount_tax_exc is used when Sellsy returns it; otherwise quantity x unit_amount is used"
        ),
        "totals": {
            "covered_sale_excl_tax": str(covered_sale),
            "purchase_excl_tax": str(purchase_total),
            "estimated_margin_excl_tax": str(margin),
            "estimated_margin_rate_percent": (
                str((margin / covered_sale * Decimal("100")).quantize(Decimal("0.01")))
                if covered_sale != 0
                else None
            ),
        },
        "coverage": {
            "rows_with_cost": included,
            "rows_missing_purchase_amount": missing_cost,
            "rows_ignored_without_computable_sale": ignored,
            "complete": missing_cost == 0
            and ignored == 0
            and not any(row["sale_basis"] == "quantity_times_price" for row in rows),
        },
        "rows": rows,
    }
