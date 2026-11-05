from __future__ import annotations

import pytest

from sellsy_mcp.errors import ToolError
from sellsy_mcp.models import CommercialDocumentInput, EstimateSearch
from sellsy_mcp.server import create_server
from sellsy_mcp.tools import _margin_analysis


class MissingNativeScope:
    async def native_search(self, *args, **kwargs):
        raise ToolError("permission_denied", "search.read missing")


class FakeSearchClient(MissingNativeScope):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def bounded_search(self, path, **kwargs):
        self.calls.append((path, kwargs))
        coverage = {
            "inspected": 1,
            "pages": 1,
            "known_total": 1,
            "complete": True,
            "truncated": False,
        }
        if path == "/companies/search":
            data = [{"id": 1002, "name": "EXAMPLE 40"}] if kwargs.get("needle") == "example 40" else []
            return {"data": data, "coverage": coverage}
        if path == "/estimates/search" and kwargs.get("needle"):
            return {"data": [], "coverage": coverage}
        if path == "/estimates/search":
            return {
                "data": [{"id": 1, "number": "DEV-203001-00001", "status": "sent"}],
                "coverage": coverage,
            }
        raise AssertionError(path)


class FakePeopleClient(MissingNativeScope):
    async def bounded_search(self, path, **kwargs):
        coverage = {
            "inspected": 1,
            "pages": 1,
            "known_total": 1,
            "complete": True,
            "truncated": False,
        }
        if path == "/companies/search":
            return {"data": [{"id": 1002, "name": "EXAMPLE 40"}], "coverage": coverage}
        if path == "/staffs/search":
            return {
                "data": [
                    {
                        "id": 12,
                        "firstname": "Alice",
                        "lastname": "Example",
                        "email": "alice@example.test",
                    }
                ],
                "coverage": coverage,
            }
        if path == "/contacts/search":
            assert kwargs["filters"] == {"companies": [1002]}
            return {
                "data": [
                    {
                        "id": 34,
                        "first_name": "Bob",
                        "last_name": "Thuard",
                        "email": "bob@example.test",
                    }
                ],
                "coverage": coverage,
            }
        if path == "/estimates/search":
            assert kwargs["filters"] == {
                "related_objects": [{"type": "company", "id": 1002}],
                "owners": [12],
                "contacts": [34],
            }
            return {"data": [{"id": 1, "number": "DEV-1"}], "coverage": coverage}
        raise AssertionError(path)


class FakeCommercialClient:
    async def list_all(self, path, **kwargs):
        return await self.get(path, **kwargs)

    async def get(self, path, **_kwargs):
        if path == "/estimates/123":
            return {"id": 123, "rows": []}
        if path in {"/estimates/123/files", "/estimates/123/payments"}:
            return {"data": [], "pagination": {"total": 0}}
        raise AssertionError(path)

    async def bounded_search(self, path, **kwargs):
        assert path == "/comments/search"
        assert kwargs["filters"] == {}
        rows = [
            {"id": 1, "description": "other", "related": [{"type": "estimate", "id": 999}]},
            {"id": 2, "description": "target", "related": [{"type": "estimate", "id": 123}]},
        ]
        matcher = kwargs.get("matcher")
        if matcher:
            rows = [row for row in rows if matcher(row)]
        return {
            "data": rows,
            "coverage": {
                "inspected": 2,
                "pages": 1,
                "known_total": 2,
                "complete": True,
                "truncated": False,
            },
        }


@pytest.mark.asyncio
async def test_estimate_text_falls_back_to_exact_company(settings, monkeypatch):
    mcp, runtime = create_server(settings)
    fake = FakeSearchClient()
    monkeypatch.setattr(runtime, "context", lambda **_kwargs: ("grant", fake))
    tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == "sellsy_search_estimates")

    result = await tool.fn(EstimateSearch(text="donne-moi le devis fait pour EXAMPLE 40"))

    assert result["data"][0]["number"] == "DEV-203001-00001"
    assert result["resolved_company_id"] == 1002
    assert result["fallback"]["strategy"] == "resolved_text_as_company"
    assert result["fallback"]["matched_query"] == "example 40"
    assert fake.calls[-1][1]["filters"] == {"related_objects": [{"type": "company", "id": 1002}]}


def test_margin_analysis_explains_partial_cost_coverage():
    result = _margin_analysis(
        {
            "rows": [
                {
                    "id": 1,
                    "quantity": "2",
                    "unit_amount": "185",
                    "purchase_amount": "150",
                    "amount_tax_exc": "370",
                },
                {"id": 2, "quantity": "1", "unit_amount": "950", "amount_tax_exc": "950"},
            ]
        }
    )

    assert result["totals"]["estimated_margin_excl_tax"] == "70"
    assert result["coverage"] == {
        "rows_with_cost": 1,
        "rows_missing_purchase_amount": 1,
        "rows_ignored_without_computable_sale": 0,
        "complete": False,
    }


@pytest.mark.asyncio
async def test_commercial_analysis_filters_comments_locally(settings, monkeypatch):
    mcp, runtime = create_server(settings)
    monkeypatch.setattr(runtime, "context", lambda **_kwargs: ("grant", FakeCommercialClient()))
    tool = next(
        tool for tool in mcp._tool_manager.list_tools() if tool.name == "sellsy_analyze_commercial_document"
    )

    result = await tool.fn(CommercialDocumentInput(document_type="estimate", document_id=123))

    assert [comment["id"] for comment in result["comments"]["data"]] == [2]


@pytest.mark.asyncio
async def test_estimate_search_resolves_author_and_company_contact(settings, monkeypatch):
    mcp, runtime = create_server(settings)
    monkeypatch.setattr(runtime, "context", lambda **_kwargs: ("grant", FakePeopleClient()))
    tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == "sellsy_search_estimates")

    result = await tool.fn(
        EstimateSearch(
            company_name="EXAMPLE 40",
            owner_name="Alice Example",
            contact_name="Bob Thuard",
        )
    )

    assert result["resolved_owner"] == {"id": 12, "name": "Alice Example"}
    assert result["resolved_contact"] == {"id": 34, "name": "Bob Thuard"}
