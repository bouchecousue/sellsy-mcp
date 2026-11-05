from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from sellsy_mcp.errors import ToolError
from sellsy_mcp.models import CompanySearch, EstimateSearch, ItemSearch
from sellsy_mcp.resolution import company_id_from, resolve_entity
from sellsy_mcp.sellsy_client import SellsyClient

BASE = "https://api.sellsy.com/v2"


def page(rows, total=None, offset=0):
    return Response(
        200,
        json={
            "data": rows,
            "pagination": {
                "total": len(rows) if total is None else total,
                "offset": offset,
                "count": len(rows),
            },
        },
    )


def native_company(name="EXAMPLE PHARMACIENS", total=1):
    return page(
        [
            {
                "id": "opaque-search-result",
                "name": name,
                "object": {"id": 1001, "type": "company", "sub_type": "client"},
            }
        ],
        total,
    )


async def test_example_uses_real_search_and_business_id(runtime_tools):
    _, _, tools = runtime_tools
    with respx.mock as router:
        route = router.get(BASE + "/search").mock(return_value=native_company())
        result = await tools["sellsy_search_companies"](CompanySearch(text="EXAMPLE"))
    assert route.calls[0].request.url.params["q"] == "EXAMPLE"
    assert route.calls[0].request.url.params["type[]"] == "company"
    assert result["data"][0]["id"] == 1001
    assert result["data"][0]["name"] == "EXAMPLE PHARMACIENS"
    assert result["source"] == "native_search"


async def test_missing_scope_scans_beyond_1000_with_echoed_offsets(runtime_tools):
    _, client, tools = runtime_tools
    client.settings.search_page_size = 100
    client.settings.search_max_pages = 50

    def companies(request):
        offset = int(request.url.params["offset"])
        rows = [
            {"id": i + 1, "name": "EXAMPLE PHARMACIENS" if i == 1321 else f"Client {i}"}
            for i in range(offset, min(offset + 100, 1498))
        ]
        return page(rows, 1498, offset)

    with respx.mock as router:
        router.get(BASE + "/search").respond(403, json={"error": {"code": "missing_scope"}})
        route = router.post(BASE + "/companies/search").mock(side_effect=companies)
        result = await tools["sellsy_search_companies"](CompanySearch(text="EXAMPLE"))
    assert result["data"][0]["id"] == 1322
    assert result["coverage"]["inspected"] == 1498
    assert result["coverage"]["complete"] and not result["coverage"]["truncated"]
    assert route.call_count == 15
    assert result["fallback"]["required_scope"] == "search.read"


async def test_overflow_resumes_without_duplicates_and_binds_query_and_user(runtime_tools):
    runtime, client, _ = runtime_tools

    def items(request):
        offset = int(request.url.params["offset"])
        return page([{"id": i + 1} for i in range(offset, min(offset + 10, 23))], 23, offset)

    with respx.mock as router:
        route = router.post(BASE + "/items/search").mock(side_effect=items)
        result = await client.bounded_search("/items/search", limit=3)
        cursor = result["next_cursor"]
        other = SellsyClient(client.settings, runtime.provider, "bob")
        with pytest.raises(ToolError):
            await other.bounded_search("/items/search", limit=3, cursor=cursor)
        with pytest.raises(ToolError):
            await client.bounded_search("/items/search", filters={"reference": "else"}, cursor=cursor)
        ids = [r["id"] for r in result["data"]]
        for _ in range(20):
            if not result["next_cursor"]:
                break
            result = await client.bounded_search("/items/search", limit=3, cursor=result["next_cursor"])
            ids.extend(r["id"] for r in result["data"])
    assert ids == list(range(1, 24))
    assert route.call_count == 3
    assert not result["coverage"]["truncated"]


async def test_stalled_pagination_pending_results_do_not_restart(runtime_tools):
    _, client, _ = runtime_tools
    with respx.mock as router:
        route = router.post(BASE + "/items/search").mock(
            side_effect=[page([{"id": 1}], 99, "abc"), page([{"id": i} for i in range(2, 7)], 99, "abc")]
        )
        result = await client.bounded_search("/items/search", limit=2, max_pages=10)
        ids = [r["id"] for r in result["data"]]
        while result["next_cursor"]:
            result = await client.bounded_search("/items/search", limit=2, cursor=result["next_cursor"])
            ids.extend(r["id"] for r in result["data"])
    assert ids == list(range(1, 7))
    assert route.call_count == 2
    assert result["status"] == "incomplete"


@pytest.mark.parametrize("total,code", [(2, "incomplete"), (1, "identity_conflict")])
async def test_identity_requires_complete_and_consistent_evidence(runtime_tools, total, code):
    _, client, _ = runtime_tools
    with respx.mock as router:
        router.get(BASE + "/search").mock(return_value=native_company(total=total))
        with pytest.raises(ToolError) as caught:
            await company_id_from(client, 123, "EXAMPLE")
    assert caught.value.code == code


async def test_ambiguous_native_candidates_not_picked(runtime_tools):
    _, client, _ = runtime_tools
    with respx.mock as router:
        router.get(BASE + "/search").mock(
            return_value=page(
                [
                    {"id": "a", "name": "Example A", "object": {"id": 1, "type": "company"}},
                    {"id": "b", "name": "Example B", "object": {"id": 2, "type": "company"}},
                ]
            )
        )
        with pytest.raises(ToolError) as caught:
            await resolve_entity(client, "companies", "Example")
    assert caught.value.code == "ambiguous"
    assert len(caught.value.details["candidates"]) == 2


async def test_catalog_hydrates_price_and_masks_costs(runtime_tools):
    _, _, tools = runtime_tools
    with respx.mock as router:
        router.get(BASE + "/search").mock(
            return_value=page(
                [{"id": "a", "name": "GMH", "object": {"id": 17, "type": "item", "sub_type": "service"}}]
            )
        )
        router.get(BASE + "/items/17").respond(
            200,
            json={
                "id": 17,
                "type": "service",
                "name": "GMH",
                "reference_price": "199",
                "purchase_amount": "99",
            },
        )
        result = await tools["sellsy_search_items"](ItemSearch(text="GMH"))
    assert result["data"][0]["reference_price"] == "199"
    assert "purchase_amount" not in json.dumps(result)


async def test_person_id_name_conflict_is_not_ignored(runtime_tools):
    _, _, tools = runtime_tools
    with respx.mock as router:
        router.get(BASE + "/search").mock(
            return_value=page([{"id": "a", "name": "Alice", "object": {"id": 42, "type": "staff"}}])
        )
        result = await tools["sellsy_search_estimates"](EstimateSearch(owner_id=99, owner_name="Alice"))
    assert result["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "query,expected", [("EXAMPLE Pharmacies", True), ("EXAMPLE Google Meet Hardware", False)]
)
async def test_company_variant_requires_every_original_word(runtime_tools, query, expected):
    _, _, tools = runtime_tools
    with respx.mock as router:

        def native(request):
            return native_company() if request.url.params["q"] == "example" else page([])

        router.get(BASE + "/search").mock(side_effect=native)
        result = await tools["sellsy_search_companies"](CompanySearch(text=query))
    assert bool(result["data"]) is expected
    if expected:
        assert result["match"]["kind"] == "approximate_name"
        assert result["data"][0]["name"] == "EXAMPLE PHARMACIENS"


def test_subscription_dates_preserve_installment_identity():
    from sellsy_mcp.presentation import _summary

    result = _summary(
        {
            "id": 1,
            "first_payment_installment_date": {"id": 17, "date": "2019-02-01"},
            "next_payment_installment_date": None,
            "invoices_generated_count": 9,
        },
        family="subscriptions",
    )
    assert result["first_payment_installment_date"] == {"id": 17, "date": "2019-02-01"}
    assert result["invoices_generated_count"] == 9


async def test_incomplete_variant_search_cannot_claim_absence(runtime_tools):
    _, _, tools = runtime_tools
    with respx.mock as router:

        def native(request):
            return native_company(total=100) if request.url.params["q"] == "example" else page([])

        router.get(BASE + "/search").mock(side_effect=native)
        result = await tools["sellsy_search_companies"](CompanySearch(text="EXAMPLE Distribution"))
    assert result["data"] == []
    assert result["status"] == "incomplete"
    assert result["coverage"]["complete"] is False
