from __future__ import annotations

import copy
import json
import time

import pytest
import respx
from httpx import ConnectError, Response
from pydantic import ValidationError

from sellsy_mcp.models import ApplyManifest, ExtraSearch, OpportunityCreate

BASE = "https://api.sellsy.com/v2"


def creation(**kwargs):
    return OpportunityCreate(
        **{"company_id": 456, "name": "Serrurerie & Accès", "pipeline_id": 10, "step_id": 20, **kwargs}
    )


def preconditions(router):
    routes = {}
    routes["company"] = router.get(BASE + "/companies/456").respond(200, json={"id": 456, "name": "Client"})
    for name, path, rows in [
        ("pipelines", "/opportunities/pipelines", [{"id": 10, "name": "Ventes"}]),
        ("steps", "/opportunities/pipelines/10/steps", [{"id": 20, "name": "Qualification"}]),
        ("contacts", "/companies/456/contacts", [{"id": 30}]),
        ("staffs", "/staffs", [{"id": 42, "status": "ok"}]),
    ]:
        routes[name] = router.get(BASE + path).respond(200, json={"data": rows, "pagination": {"total": 1}})
    routes["duplicates"] = router.post(BASE + "/opportunities/search").respond(
        200, json={"data": [], "pagination": {"total": 0}}
    )
    routes["verify"] = router.post(BASE + "/opportunities", params={"verify": "true"}).respond(204)
    return routes


def readback(payload):
    result = {
        **copy.deepcopy(payload),
        "id": 123,
        "number": "OPP-TEST",
        "company_id": 456,
        "pipeline": {"id": 10, "name": "Ventes"},
        "step": {"id": 20, "name": "Qualification"},
    }
    if "amount" in result:
        result["amount"] = {"value": "120.000", "currency": "EUR"}
    if "owner_id" in result:
        result["owner"] = {"id": result.pop("owner_id"), "type": "staff"}
    return result


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "wrong_company",
        "wrong_step",
        "lost_note",
        "wrong_amount",
        "missing_contact",
        "network",
        "readback_failure",
        "bad_id",
    ],
)
async def test_creation_checks_readback_and_never_replays(runtime_tools, case):
    runtime, _, tools = runtime_tools
    with respx.mock(assert_all_called=False) as router:
        routes = preconditions(router)
        manifest = await tools["sellsy_prepare_opportunity_create"](
            creation(
                contact_ids=[30],
                note="Mail source : https://mail.google.com/mail/#all/test",
                amount="120",
                probability=25,
                due_date="2026-10-01",
                owner_id=42,
                assigned_staff_ids=[42],
            )
        )
        assert "manifest_id" in manifest, manifest
        assert manifest["requires_explicit_approval"]
        assert routes["verify"].call_count == 1
        assert all(c.request.url.params.get("verify") == "true" for c in routes["verify"].calls)
        live = readback(manifest["payload"])
        if case == "wrong_company":
            live["company_id"] = 999
        if case == "wrong_step":
            live["step"]["id"] = 99
        if case == "lost_note":
            live["note"] = ""
        if case == "wrong_amount":
            live["amount"]["value"] = "121"
        if case == "missing_contact":
            live["contact_ids"] = []
        write = router.post(BASE + "/opportunities", params={}).respond(
            201, json={"id": True if case == "bad_id" else 123}
        )
        if case == "network":
            write.side_effect = ConnectError("connection lost")
        get = router.get(BASE + "/opportunities/123").respond(200, json=live)
        if case == "readback_failure":
            get.respond(403, json={"error": {"code": 403}})
        params = ApplyManifest(manifest_id=manifest["manifest_id"], approved=True)
        result = await tools["sellsy_apply_opportunity_create"](params)
        assert result["readback_verified"] is (case == "success"), result
        assert result["applied"] is (case == "success")
        assert result["outcome_unknown"] is (case != "success")
        persisted = runtime.manifests.read_outcome(params.manifest_id, "alice")
        assert persisted["status"] == result["status"]
        assert routes["verify"].call_count == 2
        replay = await tools["sellsy_apply_opportunity_create"](params)
        assert not replay["write_attempted"]
        assert write.call_count == 1
        assert json.loads(write.calls[0].request.content)["related"] == [{"type": "company", "id": 456}]


@pytest.mark.parametrize(
    "case,error",
    [
        ("company", "invalid_company"),
        ("pipeline", "invalid_pipeline"),
        ("step", "invalid_step"),
        ("contact", "contact_company_mismatch"),
        ("staff", "invalid_staff"),
        ("duplicate", "duplicate_opportunity"),
        ("incomplete", "incomplete"),
        ("verify", "upstream_error"),
    ],
)
async def test_invalid_preconditions_do_not_prepare_or_write(runtime_tools, case, error):
    _, _, tools = runtime_tools
    with respx.mock(assert_all_called=False) as router:
        routes = preconditions(router)
        kwargs = {}
        if case == "company":
            routes["company"].respond(200, json={"id": 456, "is_archived": True})
        if case == "pipeline":
            kwargs["pipeline_id"] = 99
        if case == "step":
            kwargs["step_id"] = 99
        if case == "contact":
            kwargs["contact_ids"] = [99]
        if case == "staff":
            kwargs["owner_id"] = 99
        if case == "duplicate":
            routes["duplicates"].respond(
                200, json={"data": [{"id": 1, "name": "  SERRURERIE & ACCES  "}], "pagination": {"total": 1}}
            )
        if case == "incomplete":
            routes["duplicates"].respond(200, json={"data": [], "pagination": {"total": 10}})
        if case == "verify":
            routes["verify"].respond(400, json={"error": {"code": 400}})
        result = await tools["sellsy_prepare_opportunity_create"](creation(**kwargs))
        assert result["error"]["code"] == error, result
        assert "manifest_id" not in result


async def test_duplicate_appearing_after_preparation_blocks_apply(runtime_tools):
    runtime, _, tools = runtime_tools
    with respx.mock(assert_all_called=False) as router:
        routes = preconditions(router)
        manifest = await tools["sellsy_prepare_opportunity_create"](creation())
        routes["duplicates"].respond(
            200, json={"data": [{"id": 9, "name": creation().name}], "pagination": {"total": 1}}
        )
        result = await tools["sellsy_apply_opportunity_create"](
            ApplyManifest(manifest_id=manifest["manifest_id"], approved=True)
        )
        assert not result["write_attempted"] and result["error"]["code"] == "duplicate_opportunity"
        runtime.manifests.inspect(manifest["manifest_id"], "alice", "opportunity.create")


@pytest.mark.parametrize("case", ["other_user", "expired", "wrong_operation", "missing_scope"])
async def test_grant_ttl_operation_and_scope_are_enforced(runtime_tools, case):
    runtime, _, tools = runtime_tools
    with respx.mock(assert_all_called=False) as router:
        preconditions(router)
        manifest = await tools["sellsy_prepare_opportunity_create"](creation())
        if case == "other_user":
            runtime.manifests.store.execute(
                "UPDATE manifests SET grant_id='bob' WHERE manifest_id=?", (manifest["manifest_id"],)
            )
        elif case == "expired":
            runtime.manifests.store.execute(
                "UPDATE manifests SET expires_at=? WHERE manifest_id=?",
                (int(time.time()) - 1, manifest["manifest_id"]),
            )
        elif case == "missing_scope":
            runtime.provider.store.execute(
                "UPDATE sellsy_grants SET scope='opportunities.read' WHERE grant_id='alice'"
            )
        tool = (
            "sellsy_apply_estimate_create" if case == "wrong_operation" else "sellsy_apply_opportunity_create"
        )
        result = await tools[tool](ApplyManifest(manifest_id=manifest["manifest_id"], approved=True))
        assert not result["write_attempted"]
        if case == "missing_scope":
            assert result["error"]["required_scope"] == "opportunities.write"
            denied = await tools["sellsy_prepare_opportunity_create"](creation())
            assert denied["error"]["code"] == "reconnect_required"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"amount": "NaN"},
        {"amount": "Infinity"},
        {"amount": "-1"},
        {"probability": 101},
        {"contact_ids": [1, 1]},
        {"contact_ids": [0]},
        {"pipeline_id": 0},
        {"due_date": "2026-02-31"},
        {"name": "  "},
        {"status": "won"},
    ],
)
def test_invalid_inputs(kwargs):
    with pytest.raises(ValidationError):
        creation(**kwargs)


async def test_step_discovery_requires_and_uses_pipeline(runtime_tools):
    _, _, tools = runtime_tools
    with respx.mock(assert_all_called=False) as router:
        preconditions(router)
        result = await tools["sellsy_search_records"](ExtraSearch(object_type="step", pipeline_id=10))
        assert result["data"][0]["id"] == 20
        invalid = await tools["sellsy_search_records"](ExtraSearch(object_type="step"))
        assert invalid["error"]["code"] == "invalid_filters"


@pytest.mark.parametrize(
    "case", ["success", "other_company", "already_linked", "changed", "missing_reciprocal"]
)
async def test_linked_estimate_is_validated_and_reread(runtime_tools, case):
    _, _, tools = runtime_tools
    estimate = {
        "id": 777,
        "status": "draft",
        "subject": "Devis",
        "rows": [],
        "related": [{"type": "company", "id": 456}],
    }
    if case == "other_company":
        estimate["related"][0]["id"] = 999
    if case == "already_linked":
        estimate["related"].append({"type": "opportunity", "id": 9})
    with respx.mock(assert_all_called=False) as router:
        preconditions(router)
        get = router.get(BASE + "/estimates/777").respond(200, json=estimate)
        manifest = await tools["sellsy_prepare_opportunity_create"](creation(main_estimate_id=777))
        if case in {"other_company", "already_linked"}:
            assert "manifest_id" not in manifest
            return
        after = copy.deepcopy(estimate)
        if case != "missing_reciprocal":
            after["related"].append({"type": "opportunity", "id": 123})
        if case == "changed":
            get.respond(200, json={**estimate, "subject": "Changed"})
        else:
            get.side_effect = [Response(200, json=estimate), Response(200, json=after)]
        write = router.post(BASE + "/opportunities", params={}).respond(201, json={"id": 123})
        router.get(BASE + "/opportunities/123").respond(200, json=readback(manifest["payload"]))
        result = await tools["sellsy_apply_opportunity_create"](
            ApplyManifest(manifest_id=manifest["manifest_id"], approved=True)
        )
        assert result["applied"] is (case == "success"), result
        assert write.call_count == (0 if case == "changed" else 1)
