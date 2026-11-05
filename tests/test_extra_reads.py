from __future__ import annotations

import io

import respx
from pypdf import PdfWriter

from sellsy_mcp.models import CompanyContacts, FileRead, IdInput, LinkedData

BASE = "https://api.sellsy.com/v2"


async def test_company_contacts_cursor_is_reusable_in_list_tool(runtime_tools):
    _, _, tools = runtime_tools
    from httpx import Response

    with respx.mock as router:
        router.get(BASE + "/companies/123").respond(200, json={"id": 123})

        def contacts(request):
            offset = int(request.url.params["offset"])
            rows = [
                {"id": i + 1, "first_name": f"Personne {i}"} for i in range(offset, min(offset + 10, 125))
            ]
            return Response(200, json={"data": rows, "pagination": {"offset": offset, "total": 125}})

        router.get(BASE + "/companies/123/contacts").mock(side_effect=contacts)
        result = await tools["sellsy_get_company"](IdInput(id=123))
        ids = [row["id"] for row in result["contacts"]]
        cursor = result["contacts_search"]["next_cursor"]
        while cursor:
            result = await tools["sellsy_list_company_contacts"](
                CompanyContacts(company_id=123, cursor=cursor)
            )
            ids.extend(row["id"] for row in result["data"])
            cursor = result["next_cursor"]
    assert ids == list(range(1, 126))


async def test_file_text_download_has_no_sellsy_bearer(runtime_tools):
    _, _, tools = runtime_tools
    with respx.mock as router:
        router.get(BASE + "/files/1").respond(
            200,
            json={
                "id": 1,
                "name": "notes",
                "extension": "txt",
                "size": 7,
                "public_link": "https://files.sellsy.com/test?signature=secret",
            },
        )
        route = router.get("https://files.sellsy.com/test").respond(200, content=b"Bonjour")
        result = await tools["sellsy_read_file"](FileRead(id=1))
    assert result["text"] == "Bonjour"
    assert "authorization" not in route.calls[0].request.headers
    assert "secret" not in str(result)


async def test_pdf_with_no_text_reports_ocr_needed(runtime_tools):
    _, _, tools = runtime_tools
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    content = io.BytesIO()
    writer.write(content)
    with respx.mock as router:
        router.get(BASE + "/files/1").respond(
            200, json={"id": 1, "public_link": "https://files.sellsy.com/test.pdf"}
        )
        router.get("https://files.sellsy.com/test.pdf").respond(200, content=content.getvalue())
        result = await tools["sellsy_read_file"](FileRead(id=1))
    assert result["ocr_required"]
    assert result["pages"] == 1
    assert not result["truncated"]


async def test_untrusted_redirect_is_not_followed(runtime_tools):
    _, _, tools = runtime_tools
    with respx.mock as router:
        router.get(BASE + "/files/1").respond(
            200, json={"id": 1, "public_link": "https://files.sellsy.com/test"}
        )
        router.get("https://files.sellsy.com/test").respond(
            302, headers={"Location": "https://127.0.0.1/secret"}
        )
        result = await tools["sellsy_read_file"](FileRead(id=1))
    assert result["error"]["code"] == "untrusted_file_host"


async def test_linked_fields_supports_pagination_and_unsupported_types_fail(runtime_tools):
    _, _, tools = runtime_tools
    with respx.mock as router:
        router.get(BASE + "/companies/123/custom-fields").respond(
            200, json={"data": [{"id": 1, "value": "A"}], "pagination": {"total": 1}}
        )
        result = await tools["sellsy_get_linked_data"](
            LinkedData(object_type="company", id=123, kind="custom_fields")
        )
        assert result["data"][0]["value"] == "A"
        invalid = await tools["sellsy_get_linked_data"](
            LinkedData(object_type="delivery", id=123, kind="smart_tags")
        )
        assert invalid["error"]["code"] == "unsupported_operation"
