from __future__ import annotations

import copy
import json

import pytest
import respx
from httpx import Response

from sellsy_mcp.models import ProductHistory

BASE = "https://api.sellsy.com/v2"


def documents():
    def doc(id, status, quantity, **kwargs):
        return {
            "id": id,
            "number": f"DOC-{id}",
            "date": "2026-09-10",
            "status": status,
            "related": [{"type": "company", "id": 123}],
            "currency": "EUR",
            "is_deposit": False,
            "is_progress_invoice": False,
            "rows": [
                {
                    "id": id * 10,
                    "type": "catalog",
                    "description": "Google Meet Hardware",
                    "reference": "GMH-1Y",
                    "quantity": quantity,
                    "unit_amount": "199",
                }
            ],
            **kwargs,
        }

    return {
        "estimates": [doc(1, "accepted", "6"), doc(2, "cancelled", "50")],
        "orders": [doc(3, "accepted", "6")],
        "invoices": [
            doc(4, "paid", "3"),
            doc(5, "paid", "3"),
            doc(6, "paid", "99", is_deposit=True),
            doc(7, "paid", "33", is_progress_invoice=True),
            doc(8, "draft", "88"),
        ],
        "credit-notes": [doc(9, "stored", "-1")],
    }


def mock_history(router, docs, forbidden=None):
    router.get(BASE + "/companies/123").respond(200, json={"id": 123, "name": "EXAMPLE"})
    for family, rows in docs.items():
        if family == forbidden:
            router.post(BASE + f"/{family}/search").respond(403, json={"error": {"code": "scope"}})
            continue

        def search(request, rows=rows):
            assert json.loads(request.content)["filters"]["related_objects"] == [
                {"type": "company", "id": 123}
            ]
            offset = int(request.url.params["offset"])
            data = [{"id": d["id"]} for d in rows[offset : offset + 10]]
            return Response(200, json={"data": data, "pagination": {"total": len(rows), "offset": offset}})

        router.post(BASE + f"/{family}/search").mock(side_effect=search)
        for doc in rows:
            router.get(BASE + f"/{family}/{doc['id']}").respond(200, json=doc)


async def test_history_separates_sources_and_keeps_renewals(runtime_tools):
    _, _, tools = runtime_tools
    params = ProductHistory(company_id=123, product_text="Google Meet Hardware", document_limit=2)
    evidence = []
    with respx.mock(assert_all_called=False) as router:
        mock_history(router, documents())
        for _ in range(10):
            result = await tools["sellsy_get_customer_product_history"](params)
            assert "error" not in result, result
            evidence.extend(result["evidence"])
            if not result["next_cursor"]:
                break
            params.cursor = result["next_cursor"]
    assert result["coverage"]["complete"]
    assert result["quantities_by_bucket"] == {
        "accepted_estimates": "6",
        "accepted_orders": "6",
        "invoices": "6",
        "deposits": "99",
        "progress_invoices": "33",
        "credit_notes": "1",
    }
    assert result["net_invoiced_line_quantity"] == "5"
    assert result["active_license_quantity"] is None
    assert len(evidence) == 7
    assert len({(e["document_type"], e["document_id"], e["row_id"]) for e in evidence}) == len(evidence)


@pytest.mark.parametrize("fault", ["credit_scope", "missing_rows", "wrong_customer"])
async def test_history_incomplete_never_returns_final_net(runtime_tools, fault):
    _, _, tools = runtime_tools
    docs = copy.deepcopy(documents())
    if fault == "missing_rows":
        del docs["invoices"][0]["rows"]
    if fault == "wrong_customer":
        docs["invoices"][0]["related"][0]["id"] = 456
    with respx.mock(assert_all_called=False) as router:
        mock_history(router, docs, "credit-notes" if fault == "credit_scope" else None)
        result = await tools["sellsy_get_customer_product_history"](
            ProductHistory(company_id=123, references=["GMH-1Y"])
        )
    assert result["status"] == "incomplete"
    assert result["net_invoiced_line_quantity"] is None
    assert result["errors"]
    assert result["quantities_by_bucket"]["accepted_estimates"] == "6"


async def test_history_large_evidence_is_resumable(runtime_tools):
    _, _, tools = runtime_tools
    docs = {"estimates": [], "orders": [], "credit-notes": [], "invoices": [documents()["invoices"][0]]}
    template = docs["invoices"][0]["rows"][0]
    docs["invoices"][0]["rows"] = [{**template, "id": i} for i in range(450)]
    params = ProductHistory(company_id=123, item_ids=[], references=["gmh-1y"])
    sizes = []
    with respx.mock as router:
        mock_history(router, docs)
        for _ in range(5):
            result = await tools["sellsy_get_customer_product_history"](params)
            sizes.append(len(result["evidence"]))
            if not result["next_cursor"]:
                break
            params.cursor = result["next_cursor"]
    assert sizes == [200, 200, 50]
    assert result["coverage"]["complete"]
    assert result["net_invoiced_line_quantity"] == "1350"
