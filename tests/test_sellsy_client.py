from __future__ import annotations

import pytest
import respx
from httpx import Response

from sellsy_mcp.sellsy_client import SellsyClient


class FakeProvider:
    async def get_sellsy_access_token(self, grant_id):
        return f"token-for-{grant_id}", {"staff_id": grant_id}


async def test_bounded_text_search_reports_partial_coverage(settings):
    client = SellsyClient(settings, FakeProvider(), "alice")
    first = [{"id": index, "subject": f"Projet {index}"} for index in range(10)]
    second = [
        {"id": 10 + index, "subject": "Silverfin vidéo" if index == 3 else "Autre"} for index in range(10)
    ]
    with respx.mock(assert_all_called=True) as router:
        route = router.post(f"{str(settings.sellsy_api_base_url).rstrip('/')}/estimates/search")
        route.side_effect = [
            Response(200, json={"data": first, "pagination": {"total": 50}}),
            Response(200, json={"data": second, "pagination": {"total": 50}}),
        ]
        result = await client.bounded_search(
            "/estimates/search",
            needle="silverfin video",
            fields=("subject",),
            max_pages=2,
        )
    assert [row["id"] for row in result["data"]] == [13]
    assert result["coverage"] == {
        "inspected": 20,
        "pages": 2,
        "known_total": 50,
        "complete": False,
        "truncated": True,
    }


async def test_final_page_can_be_complete_but_results_truncated(settings):
    client = SellsyClient(settings, FakeProvider(), "alice")
    with respx.mock as router:
        route = router.post(f"{str(settings.sellsy_api_base_url).rstrip('/')}/items/search")
        route.return_value = Response(200, json={"data": [{"id": i} for i in range(5)], "total": 5})
        result = await client.bounded_search("/items/search", limit=2)
    assert len(result["data"]) == 2
    assert result["coverage"] == {
        "inspected": 5,
        "pages": 1,
        "known_total": 5,
        "complete": True,
        "truncated": True,
    }


async def test_cap_matching_exact_total_is_not_truncated(settings):
    client = SellsyClient(settings, FakeProvider(), "alice")
    with respx.mock as router:
        router.post(f"{str(settings.sellsy_api_base_url).rstrip('/')}/items/search").return_value = Response(
            200,
            json={"data": [{"id": i} for i in range(10)], "pagination": {"total": 10}},
        )
        result = await client.bounded_search("/items/search", limit=10)
    assert result["coverage"]["complete"] is True
    assert result["coverage"]["truncated"] is False


async def test_empty_search_preserves_zero_total(settings):
    client = SellsyClient(settings, FakeProvider(), "alice")
    with respx.mock as router:
        router.post(f"{str(settings.sellsy_api_base_url).rstrip('/')}/items/search").return_value = Response(
            200,
            json={"data": [], "pagination": {"total": 0}},
        )
        result = await client.bounded_search("/items/search")
    assert result["coverage"]["known_total"] == 0
    assert result["coverage"]["complete"] is True
    assert result["coverage"]["truncated"] is False


async def test_search_follows_opaque_cursor_even_on_short_page(settings):
    client = SellsyClient(settings, FakeProvider(), "alice")
    with respx.mock as router:
        route = router.post(f"{str(settings.sellsy_api_base_url).rstrip('/')}/items/search")
        route.side_effect = [
            Response(200, json={"data": [{"id": 1}], "pagination": {"offset": "abc+/=", "total": 2}}),
            Response(200, json={"data": [{"id": 2}], "pagination": {"total": 2}}),
        ]
        result = await client.bounded_search("/items/search")
    assert route.calls[1].request.url.params["offset"] == "abc+/="
    assert [row["id"] for row in result["data"]] == [1, 2]
    assert result["coverage"]["complete"] is True


async def test_repeated_cursor_stops_with_partial_coverage(settings):
    client = SellsyClient(settings, FakeProvider(), "alice")
    with respx.mock as router:
        route = router.post(f"{str(settings.sellsy_api_base_url).rstrip('/')}/items/search")
        route.return_value = Response(
            200,
            json={
                "data": [{"id": 1}],
                "pagination": {"offset": "repeated", "total": 50},
            },
        )
        result = await client.bounded_search("/items/search", max_pages=10)
    assert route.call_count == 2
    assert result["coverage"]["complete"] is False


async def test_empty_page_below_known_total_is_not_complete(settings):
    client = SellsyClient(settings, FakeProvider(), "alice")
    with respx.mock as router:
        router.post(f"{str(settings.sellsy_api_base_url).rstrip('/')}/items/search").return_value = Response(
            200,
            json={"data": [], "pagination": {"total": 10}},
        )
        result = await client.bounded_search("/items/search")
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["truncated"] is True


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": -1}, {"max_pages": 0}])
async def test_invalid_bounds_are_rejected_before_request(settings, kwargs):
    client = SellsyClient(settings, FakeProvider(), "alice")
    with pytest.raises(ValueError):
        await client.bounded_search("/items/search", **kwargs)


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": {}}, {"data": ["invalid"]}])
async def test_invalid_search_payload_is_not_reported_as_empty_complete_result(settings, payload):
    client = SellsyClient(settings, FakeProvider(), "alice")
    with respx.mock as router:
        router.post(f"{str(settings.sellsy_api_base_url).rstrip('/')}/items/search").return_value = Response(
            200,
            json=payload,
        )
        with pytest.raises(RuntimeError, match="list of objects"):
            await client.bounded_search("/items/search")
