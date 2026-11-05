from __future__ import annotations

import copy
import json

import pytest
import respx
from httpx import ConnectError, Response
from pydantic import ValidationError
from test_opportunity_create import BASE, preconditions, readback

from sellsy_mcp.models import ApplyManifest, OpportunityUpdate


def current():
    return readback(
        {
            "name": "Projet",
            "status": "open",
            "note": "Conserver le contexte",
            "amount": "120",
            "probability": 25,
            "due_date": "2026-11-01",
            "owner_id": 42,
            "contact_ids": [30],
            "assigned_staff_ids": [42],
            "related": [{"type": "company", "id": 456}],
            "main_doc_id": 777,
        }
    )


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "stale",
        "lost_note",
        "wrong_amount",
        "lost_document",
        "network",
        "readback_failure",
    ],
)
async def test_update_preserves_fields_verifies_and_never_replays(runtime_tools, case):
    runtime, _, tools = runtime_tools
    before = current()
    with respx.mock(assert_all_called=False) as router:
        preconditions(router)
        get = router.get(BASE + "/opportunities/123").respond(200, json=before)
        verify = router.put(BASE + "/opportunities/123", params={"verify": "true"}).respond(204)
        write = router.put(BASE + "/opportunities/123", params={}).respond(204)
        manifest = await tools["sellsy_prepare_opportunity_update"](
            OpportunityUpdate(
                opportunity_id=123,
                amount="240.50",
                status="won",
                due_date=None,
            )
        )
        assert "manifest_id" in manifest, manifest
        assert manifest["requires_explicit_approval"]
        assert manifest["diff"]["due_date"] == {"before": "2026-11-01", "after": None}
        assert not write.called
        after = copy.deepcopy(before)
        after.update(status="won", due_date=None, amount={"value": "240.500", "currency": "EUR"})
        if case == "lost_note":
            after["note"] = ""
        if case == "wrong_amount":
            after["amount"]["value"] = "241"
        if case == "lost_document":
            after["main_doc_id"] = None
        if case == "stale":
            get.respond(200, json={**before, "note": "Modification concurrente"})
        elif case == "readback_failure":
            get.side_effect = [Response(200, json=before), Response(403, json={"error": {"code": 403}})]
        else:
            get.side_effect = [Response(200, json=before), Response(200, json=after)]
        if case == "network":
            write.side_effect = ConnectError("lost")
        params = ApplyManifest(manifest_id=manifest["manifest_id"], approved=True)
        result = await tools["sellsy_apply_opportunity_update"](params)
        assert result["applied"] is (case == "success"), result
        assert result["write_attempted"] is (case != "stale")
        assert verify.call_count == (1 if case == "stale" else 2)
        if case == "stale":
            assert result["error"]["code"] == "stale_manifest"
            assert not write.called
            return
        assert write.call_count == 1
        payload = json.loads(write.calls[0].request.content)
        assert payload == {"amount": "240.50", "status": "won", "due_date": None, "owner_id": 42}
        stored = runtime.manifests.read_outcome(params.manifest_id, "alice")
        assert stored["status"] == result["status"]
        retry = await tools["sellsy_apply_opportunity_update"](params)
        assert not retry["write_attempted"]
        assert write.call_count == 1


@pytest.mark.parametrize(
    "case,error",
    [
        ("wrong_step", "invalid_step"),
        ("wrong_contact", "contact_company_mismatch"),
        ("wrong_staff", "invalid_staff"),
        ("verify", "upstream_error"),
        ("no_changes", "no_changes"),
        ("missing_scope", "reconnect_required"),
    ],
)
async def test_update_preconditions(runtime_tools, case, error):
    runtime, _, tools = runtime_tools
    with respx.mock(assert_all_called=False) as router:
        preconditions(router)
        router.get(BASE + "/opportunities/123").respond(200, json=current())
        router.put(BASE + "/opportunities/123", params={"verify": "true"}).respond(
            400 if case == "verify" else 204,
            json={"error": {"code": 400}} if case == "verify" else None,
        )
        fields = {"note": "Nouvelle note"}
        if case == "wrong_step":
            fields = {"step_id": 99}
        if case == "wrong_contact":
            fields = {"contact_ids": [99]}
        if case == "wrong_staff":
            fields = {"owner_id": 99}
        if case == "no_changes":
            fields = {"amount": "120.00"}
        if case == "missing_scope":
            runtime.provider.store.execute("UPDATE sellsy_grants SET scope='opportunities.read'")
        result = await tools["sellsy_prepare_opportunity_update"](
            OpportunityUpdate(opportunity_id=123, **fields)
        )
        assert result["error"]["code"] == error, result
        assert "manifest_id" not in result


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"note": None},
        {"name": " "},
        {"amount": "NaN"},
        {"amount": "-1"},
        {"contact_ids": [30, 30]},
        {"pipeline_id": 11},
        {"status": "unknown"},
        {"company_id": 999},
        {"main_doc_id": 999},
        {"assigned_staff_ids": None},
    ],
)
def test_update_rejects_invalid_or_unsupported_changes(fields):
    with pytest.raises(ValidationError):
        OpportunityUpdate(opportunity_id=123, **fields)


async def test_update_clears_lists_and_note_but_preserves_omitted_fields(runtime_tools):
    _, _, tools = runtime_tools
    with respx.mock(assert_all_called=False) as router:
        preconditions(router)
        router.get(BASE + "/opportunities/123").respond(200, json=current())
        router.put(BASE + "/opportunities/123", params={"verify": "true"}).respond(204)
        result = await tools["sellsy_prepare_opportunity_update"](
            OpportunityUpdate(
                opportunity_id=123,
                contact_ids=[],
                assigned_staff_ids=[],
                note="",
            )
        )
        assert result["payload"] == {
            "contact_ids": [],
            "assigned_staff_ids": [],
            "note": "",
            "status": "open",
            "owner_id": 42,
        }
