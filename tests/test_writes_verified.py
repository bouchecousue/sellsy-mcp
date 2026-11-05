from __future__ import annotations

import copy
import json

import pytest
import respx
from httpx import ConnectError, Response

from sellsy_mcp.models import ApplyManifest, EstimateCreate, EstimateRowPatch, EstimateUpdate

BASE = "https://api.sellsy.com/v2"


def draft():
    return {
        "id": 123,
        "status": "draft",
        "subject": "Réunion",
        "related": [{"type": "company", "id": 456}],
        "rows": [
            {
                "id": 1,
                "type": "single",
                "description": "Licences",
                "quantity": "1",
                "unit_amount": "99",
                "tax_id": 1,
            },
            {
                "id": 2,
                "type": "single",
                "description": "Installation",
                "quantity": "2",
                "unit_amount": "100",
                "tax_id": 1,
            },
        ],
    }


def taxes(router, active=True):
    router.get(BASE + "/taxes").respond(
        200, json={"data": [{"id": 1, "is_active": active}], "pagination": {"total": 1}}
    )


@pytest.mark.parametrize(
    "case", ["success", "ignored_put", "readback_failure", "network_failure", "lost_untouched_row"]
)
async def test_update_verified_or_durable_unknown_without_replay(runtime_tools, case):
    runtime, _, tools = runtime_tools
    before = draft()
    after = copy.deepcopy(before)
    after["rows"][0]["quantity"] = "9.000"
    if case == "ignored_put":
        after = before
    if case == "lost_untouched_row":
        after["rows"].pop()
    with respx.mock(assert_all_called=False) as router:
        taxes(router)
        get = router.get(BASE + "/estimates/123").mock(return_value=Response(200, json=before))
        manifest = await tools["sellsy_prepare_estimate_update"](
            EstimateUpdate(estimate_id=123, rows=[EstimateRowPatch(id=1, quantity="9")])
        )
        assert "manifest_id" in manifest, manifest
        get.side_effect = [Response(200, json=before), Response(200, json=after)]
        if case == "readback_failure":
            get.side_effect = [Response(200, json=before)] + [
                Response(403, json={"error": {"code": "scope"}})
            ] * 3
        put = router.put(BASE + "/estimates/123").mock(return_value=Response(200, json=after))
        if case == "network_failure":
            put.side_effect = ConnectError("connection lost")
        params = ApplyManifest(manifest_id=manifest["manifest_id"], approved=True)
        result = await tools["sellsy_apply_estimate_update"](params)
        if case == "success":
            assert result["applied"] and result["readback_verified"], result
            assert not result["outcome_unknown"]
        else:
            assert not result["applied"] and result["outcome_unknown"], result
        persisted = runtime.manifests.read_outcome(params.manifest_id, "alice")
        assert persisted["object_id"] == 123
        assert persisted["status"] == result["status"]
        # Replay must never issue a second write, even when the response was lost.
        replay = await tools["sellsy_apply_estimate_update"](params)
        assert not replay["write_attempted"]
        assert put.call_count == 1
        sent = json.loads(put.calls[0].request.content)
        assert [row["id"] for row in sent["rows"]] == [1, 2]


async def test_wrong_operation_and_stale_preconditions_do_not_consume_manifest(runtime_tools):
    runtime, _, tools = runtime_tools
    with respx.mock as router:
        taxes(router)
        get = router.get(BASE + "/estimates/123").mock(return_value=Response(200, json=draft()))
        manifest = await tools["sellsy_prepare_estimate_update"](
            EstimateUpdate(estimate_id=123, rows=[EstimateRowPatch(id=1, quantity="9")])
        )
        params = ApplyManifest(manifest_id=manifest["manifest_id"], approved=True)
        wrong = await tools["sellsy_apply_estimate_create"](params)
        assert not wrong["write_attempted"]
        runtime.manifests.inspect(params.manifest_id, "alice", "estimate.update")
        get.return_value = Response(200, json={**draft(), "subject": "Modifié depuis"})
        stale = await tools["sellsy_apply_estimate_update"](params)
        assert stale["error"]["code"] == "stale_manifest"
        runtime.manifests.inspect(params.manifest_id, "alice", "estimate.update")


@pytest.mark.parametrize("case", ["wrong_subject", "wrong_company", "success"])
async def test_creation_compares_approved_result(runtime_tools, case):
    _, _, tools = runtime_tools
    params = EstimateCreate(
        company_id=456,
        subject="Réunion",
        rows=[
            {"type": "single", "description": "Licence", "quantity": "1", "unit_amount": "99", "tax_id": 1}
        ],
    )
    with respx.mock as router:
        taxes(router)
        router.get(BASE + "/companies/456").respond(200, json={"id": 456, "name": "Client"})
        manifest = await tools["sellsy_prepare_estimate_create"](params)
        assert "manifest_id" in manifest, manifest
        expected = copy.deepcopy(manifest["payload"])
        expected["id"] = 123
        expected["rows"][0]["id"] = 1
        if case == "wrong_subject":
            expected["subject"] = "Autre"
        if case == "wrong_company":
            expected["related"][0]["id"] = 999
        router.post(BASE + "/estimates").respond(201, json={"id": 123})
        router.get(BASE + "/estimates/123").respond(200, json=expected)
        result = await tools["sellsy_apply_estimate_create"](
            ApplyManifest(manifest_id=manifest["manifest_id"], approved=True)
        )
    assert result["applied"] is (case == "success"), result
    assert result["readback_verified"] is (case == "success")


@pytest.mark.parametrize(
    "case,error",
    [
        ("tax", "invalid_tax"),
        ("contact", "contact_company_mismatch"),
        ("address", "address_company_mismatch"),
    ],
)
async def test_creation_rejects_invalid_business_links(runtime_tools, case, error):
    _, _, tools = runtime_tools
    kwargs = (
        {"contact_id": 999}
        if case == "contact"
        else ({"delivery_address_id": 999} if case == "address" else {})
    )
    params = EstimateCreate(
        company_id=456,
        subject="Réunion",
        rows=[
            {"type": "single", "description": "Licence", "quantity": "1", "unit_amount": "99", "tax_id": 1}
        ],
        **kwargs,
    )
    with respx.mock(assert_all_called=False) as router:
        taxes(router, active=False)
        router.get(BASE + "/companies/456").respond(200, json={"id": 456})
        router.get(BASE + "/companies/456/contacts").respond(
            200, json={"data": [{"id": 42}], "pagination": {"total": 1}}
        )
        router.get(BASE + "/companies/456/addresses").respond(
            200, json={"data": [{"id": 42}], "pagination": {"total": 1}}
        )
        result = await tools["sellsy_prepare_estimate_create"](params)
    assert result["error"]["code"] == error
    assert "manifest_id" not in result


@pytest.mark.parametrize("section", ["commercial_header", "rows", "other_fields", "legacy"])
async def test_stale_diagnostics_keep_guard_and_hide_business_values(runtime_tools, caplog, section):
    import logging

    runtime, _, tools = runtime_tools
    before = draft()
    after = copy.deepcopy(before)
    if section == "rows":
        after["rows"][0]["quantity"] = "123456"
    elif section == "other_fields":
        after["private_customer_metadata"] = "never-log-this-business-value"
    else:
        after["subject"] = "never-log-this-business-value"
    with respx.mock(assert_all_called=False) as router, caplog.at_level(logging.INFO):
        taxes(router)
        get = router.get(BASE + "/estimates/123").respond(200, json=before)
        prepared = await tools["sellsy_prepare_estimate_update"](
            EstimateUpdate(estimate_id=123, rows=[EstimateRowPatch(id=1, quantity="9")])
        )
        if section == "legacy":
            checks = runtime.manifests.inspect(prepared["manifest_id"], "alice")["checks"]
            checks.pop("before_sections")
            runtime.provider.store.execute(
                "UPDATE manifest_checks SET checks_json=? WHERE manifest_id=?",
                (json.dumps(checks), prepared["manifest_id"]),
            )
        get.return_value = Response(200, json=after)
        put = router.put(BASE + "/estimates/123").respond(200, json=after)
        result = await tools["sellsy_apply_estimate_update"](
            ApplyManifest(manifest_id=prepared["manifest_id"], approved=True)
        )
        assert result["error"]["code"] == "stale_manifest"
        expected = "unavailable" if section == "legacy" else section
        assert result["error"]["changed_sections"] == [expected]
        assert not result["write_attempted"] and put.call_count == 0
        assert f"changed_sections={expected}" in caplog.text
        assert "never-log-this-business-value" not in caplog.text + json.dumps(result)
        assert "private_customer_metadata" not in caplog.text + json.dumps(result)
        assert "123456" not in caplog.text + json.dumps(result)
