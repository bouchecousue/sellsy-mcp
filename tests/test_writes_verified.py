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


@pytest.mark.parametrize("case", ["success", "old_id", "duplicate_id", "wrong_order", "wrong_subject"])
async def test_combined_estimate_changes_match_approved_preview(runtime_tools, case):
    _, _, tools = runtime_tools
    before = draft()
    before["rows"].append({"id": 3, "type": "title", "text": "Prestation"})
    before["rows"][1]["purchase_amount"] = "40"
    new_row = {
        "type": "catalog",
        "item_id": 7,
        "description": "Maintenance annuelle",
        "quantity": "1",
        "unit_amount": "60",
        "tax_id": 1,
    }
    addition = {key: value for key, value in new_row.items() if key != "item_id"}
    addition["related"] = {"type": "service", "id": 7}
    params = EstimateUpdate(
        estimate_id=123,
        subject="Maintenance",
        rows=[{"id": 1, "quantity": "3"}],
        add_rows=[new_row],
        remove_row_ids=[2],
    )
    after = copy.deepcopy(before)
    after["subject"] = params.subject
    after["rows"].pop(1)
    after["rows"][0]["quantity"] = "3.000"
    after["rows"].append({**addition, "id": 77})
    if case == "old_id":
        after["rows"][-1]["id"] = 2
    elif case == "duplicate_id":
        after["rows"][-1]["id"] = 3
    elif case == "wrong_order":
        after["rows"].reverse()
    elif case == "wrong_subject":
        after["subject"] = before["subject"]

    with respx.mock as router:
        taxes(router)
        router.get(BASE + "/items/7").respond(200, json={"id": 7, "type": "service"})
        get = router.get(BASE + "/estimates/123").respond(200, json=before)
        put = router.put(BASE + "/estimates/123").respond(200, json={})
        preview = await tools["sellsy_prepare_estimate_update"](params)
        assert not put.called
        assert preview["requires_explicit_approval"]
        assert preview["diff"] == [
            {"field": "subject", "before": "Réunion", "after": "Maintenance"},
            {"row_id": 1, "changes": {"quantity": {"before": "1", "after": "3"}}},
            {
                "operation": "remove",
                "row_id": 2,
                "before": {k: v for k, v in before["rows"][1].items() if k != "purchase_amount"},
            },
            {"operation": "add", "after": addition},
        ]
        get.side_effect = [Response(200, json=before), Response(200, json=after)]
        approval = ApplyManifest(manifest_id=preview["manifest_id"], approved=True)
        result = await tools["sellsy_apply_estimate_update"](approval)
        assert json.loads(put.calls[0].request.content) == {
            "subject": "Maintenance",
            "rows": [{"id": 1, "quantity": "3", "description": "Licences"}, {"id": 3}, addition],
        }
        assert result["readback_verified"] is (case == "success"), result
        if case != "success":
            assert result["status"] == "readback_mismatch"
        replay = await tools["sellsy_apply_estimate_update"](approval)
        assert not replay["write_attempted"]
        assert put.call_count == 1


@pytest.mark.parametrize("case", ["subject", "remove_all", "add_first"])
async def test_independent_draft_edits(runtime_tools, case):
    _, _, tools = runtime_tools
    before = draft()
    after = copy.deepcopy(before)
    if case == "subject":
        changes = payload = {"subject": "Support technique"}
        after.update(payload)
    elif case == "remove_all":
        changes = {"remove_row_ids": [1, 2]}
        payload = {"rows": []}
        after["rows"] = []
    else:
        before["rows"] = None
        row = {k: v for k, v in after["rows"][0].items() if k != "id"}
        changes = {"add_rows": [row]}
        payload = {"rows": [row]}
        after["rows"] = [{**row, "id": 5}]
    with respx.mock as router:
        if case == "add_first":
            taxes(router)
        get = router.get(BASE + "/estimates/123").respond(200, json=before)
        prepared = await tools["sellsy_prepare_estimate_update"](EstimateUpdate(estimate_id=123, **changes))
        assert prepared["payload"] == payload
        put = router.put(BASE + "/estimates/123").respond(200, json={})
        get.side_effect = [Response(200, json=before), Response(200, json=after)]
        result = await tools["sellsy_apply_estimate_update"](
            ApplyManifest(manifest_id=prepared["manifest_id"], approved=True)
        )
        assert result["readback_verified"], result
        assert json.loads(put.calls[0].request.content) == payload


@pytest.mark.parametrize("case", ["sent", "unknown_row", "unchanged", "tax", "hidden_cost", "missing_rows"])
async def test_invalid_update_has_no_manifest(runtime_tools, case):
    _, _, tools = runtime_tools
    before = draft()
    row = {k: v for k, v in before["rows"][0].items() if k != "id"}
    changes = {"subject": "Support"}
    if case == "sent":
        before["status"] = "sent"
    elif case == "unknown_row":
        changes = {"remove_row_ids": [9]}
    elif case == "unchanged":
        changes = {"subject": before["subject"]}
    elif case == "missing_rows":
        del before["rows"]
    elif case in {"tax", "hidden_cost"}:
        if case == "hidden_cost":
            row["purchase_amount"] = "5"
        changes = {"add_rows": [row]}
    with respx.mock(assert_all_called=False) as router:
        taxes(router, active=False)
        router.get(BASE + "/estimates/123").respond(200, json=before)
        result = await tools["sellsy_prepare_estimate_update"](EstimateUpdate(estimate_id=123, **changes))
        assert "error" in result and "manifest_id" not in result, result


@pytest.mark.parametrize(
    "changes", [{}, {"remove_row_ids": [1, 1]}, {"rows": [{"id": 1, "quantity": "2"}], "remove_row_ids": [1]}]
)
def test_update_requires_unambiguous_changes(changes):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EstimateUpdate(estimate_id=123, **changes)


async def test_catalog_addition_rechecked_before_write(runtime_tools):
    _, _, tools = runtime_tools
    before = draft()
    new_row = {k: v for k, v in before["rows"][0].items() if k != "id"}
    new_row.update(type="catalog", item_id=7, related_type="service")
    with respx.mock as router:
        taxes(router)
        item = router.get(BASE + "/items/7").respond(200, json={"id": 7, "type": "service"})
        router.get(BASE + "/estimates/123").respond(200, json=before)
        prepared = await tools["sellsy_prepare_estimate_update"](
            EstimateUpdate(estimate_id=123, add_rows=[new_row])
        )
        item.respond(200, json={"id": 7, "type": "service", "is_archived": True})
        result = await tools["sellsy_apply_estimate_update"](
            ApplyManifest(manifest_id=prepared["manifest_id"], approved=True)
        )
        assert result["error"]["code"] == "invalid_catalog_item" and not result["write_attempted"]
        assert item.call_count == 2
