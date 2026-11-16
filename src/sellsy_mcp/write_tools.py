from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from .errors import ToolError, error_result
from .manifests import changed_sections, fingerprint, section_fingerprints
from .models import (
    ApplyManifest,
    EstimateCreate,
    EstimateStatusUpdate,
    EstimateUpdate,
    ManifestStatus,
    OpportunityCreate,
    OpportunityUpdate,
)
from .opportunity_validation import (
    has_relation,
    opportunity_mismatches,
    opportunity_snapshot,
    require_opportunity_scope,
    validate_opportunity_creation,
    validate_opportunity_update,
)
from .presentation import _without_costs
from .runtime import READ_HINTS, WRITE_HINTS, Runtime
from .write_validation import (
    estimate_update_mismatches,
    estimate_update_plan,
    mismatches,
    new_estimate_rows,
    snapshot,
    validate_creation,
    validate_estimate_rows,
    validate_transition,
)


async def apply_operation(runtime: Runtime, params: ApplyManifest, operation: str) -> dict[str, Any]:
    grant_id, client = runtime.context(write=True)
    object_type = "opportunity" if operation.startswith("opportunity.") else "estimate"
    collection = "/opportunities" if object_type == "opportunity" else "/estimates"
    result: dict[str, Any] = {
        "manifest_id": params.manifest_id,
        "applied": False,
        "write_attempted": False,
        "write_applied": None,
        "readback_verified": False,
        "outcome_unknown": False,
    }
    identity: dict[str, Any] = {}
    claimed = False
    try:
        manifest = runtime.manifests.inspect(params.manifest_id, grant_id, operation)
        _, identity = await runtime.provider.get_sellsy_access_token(grant_id)
        object_id = int(manifest["object_id"]) if manifest["object_id"] else None
        result["object_id"] = object_id
        if operation == "opportunity.create":
            require_opportunity_scope(runtime, grant_id)
            checked = await validate_opportunity_creation(client, manifest["payload"])
            if checked["estimate_fingerprint"] != manifest["checks"].get("estimate_fingerprint"):
                raise ToolError("stale_manifest", "Le devis lié a changé ; préparer un nouveau manifeste.")
            expected = manifest["payload"]
        elif operation == "opportunity.update":
            require_opportunity_scope(runtime, grant_id)
            before = await client.get(f"{collection}/{object_id}")
            if fingerprint(before) != manifest["before_fingerprint"]:
                raise ToolError(
                    "stale_manifest",
                    "L'opportunité a changé ; préparer un nouveau manifeste.",
                    changed_sections=changed_sections(manifest["checks"], before),
                )
            expected = manifest["checks"]["expected"]
            await validate_opportunity_update(client, object_id, manifest["payload"], expected)
        elif operation == "estimate.create":
            await validate_creation(client, manifest["payload"])
            expected = manifest["payload"]
        else:
            if operation == "estimate.update":
                await validate_estimate_rows(client, manifest["payload"].get("rows", []))
            before = await client.get(f"/estimates/{object_id}")
            if fingerprint(before) != manifest["before_fingerprint"]:
                raise ToolError(
                    "stale_manifest",
                    "Le devis a changé ; préparer un nouveau manifeste.",
                    changed_sections=changed_sections(manifest["checks"], before),
                )
            if operation == "estimate.update":
                if before.get("status") != "draft":
                    raise ToolError("not_editable", "Seuls les devis brouillons sont modifiables.")
                expected = manifest["checks"].get("expected")
                if not expected:
                    raise ToolError(
                        "legacy_manifest", "Préparer un nouveau manifeste avec vérification des lignes."
                    )
            else:
                validate_transition(before.get("status"), manifest["payload"]["status"])
                expected = {"status": manifest["payload"]["status"]}
        runtime.manifests.claim(params.manifest_id, grant_id, operation)
        claimed = True
        # Persist intent before contacting Sellsy; a process crash must not suggest replay is safe.
        result.update(status="write_outcome_unknown", write_attempted=True, outcome_unknown=True)
        runtime.manifests.outcome(params.manifest_id, grant_id, result)
        if operation in {"estimate.create", "opportunity.create"}:
            created = await client.post(collection, manifest["payload"])
            object_id = created.get("id") or (created.get("data") or {}).get("id")
            if isinstance(object_id, bool) or not isinstance(object_id, int) or object_id <= 0:
                raise RuntimeError("Create response omitted a valid object ID")
            result["object_id"] = object_id
        else:
            suffix = "/status" if operation == "estimate.status.update" else ""
            await client.put(f"{collection}/{object_id}{suffix}", manifest["payload"])
        result["write_response_received"] = True
        runtime.manifests.outcome(params.manifest_id, grant_id, result)
        live = await client.get(f"{collection}/{object_id}")
        if operation == "estimate.update":
            issues = estimate_update_mismatches(expected, live, before)
        elif object_type == "opportunity":
            issues = opportunity_mismatches(expected, live)
        else:
            issues = mismatches(expected, live)
        if operation == "opportunity.create" and expected.get("main_doc_id"):
            estimate = await client.get(f"/estimates/{expected['main_doc_id']}")
            if not has_relation(estimate, "opportunity", object_id):
                issues.append("estimate.related.opportunity")
            # Linking may change metadata/relations, but must preserve the approved commercial content.
            before_link = manifest["checks"]["estimate_snapshot"]
            after_link = snapshot(estimate)
            after_link["related"] = [
                r for r in after_link.get("related", []) if r.get("type") != "opportunity"
            ]
            issues.extend(f"estimate.{field}" for field in mismatches(before_link, after_link))
        if live.get("id") != object_id:
            issues.append("id")
        if issues:
            result.update(
                status="readback_mismatch",
                mismatched_fields=issues,
                next_action="Vérifier l'objet relu ; ne pas rejouer cette écriture.",
            )
        else:
            result.update(
                status="verified",
                applied=True,
                write_applied=True,
                readback_verified=True,
                outcome_unknown=False,
                number=live.get("number"),
            )
        runtime.manifests.outcome(params.manifest_id, grant_id, result)
        response = {**result, object_type: _without_costs(live)}
    except Exception as exc:
        result.update(error=error_result(exc)["error"])
        if not result["write_attempted"]:
            result["status"] = "not_applied"
            result["write_applied"] = False
        else:
            result["next_action"] = "Relire l'objet ou consulter le résultat du manifeste ; ne pas rejouer."
        if claimed:
            runtime.manifests.outcome(params.manifest_id, grant_id, result)
        response = result
    try:
        runtime.audit.write(
            {
                **identity,
                "tool": operation,
                "object_type": object_type,
                "object_id": result.get("object_id"),
                "manifest_id": params.manifest_id,
                "result": result.get("status"),
                "error_code": result.get("error", {}).get("code"),
            }
        )
    except OSError:
        response["audit_warning"] = "Écriture du journal indisponible ; consulter le résultat persisté."
    return response


def register_estimate_tools(mcp: MCPServer, runtime: Runtime) -> None:
    @runtime.tool(
        mcp,
        name="sellsy_prepare_opportunity_update",
        title="Préparer la modification d'une opportunité",
        annotations=READ_HINTS,
    )
    async def prepare_opportunity_update(params: OpportunityUpdate) -> dict[str, Any]:
        """Prépare nom, note, montant, échéance, statut, pipeline/étape, responsable ou contacts.
        Champs omis conservés ; note remplace la note complète ; listes vides effacent les affectations.
        due_date=null efface l'échéance. Client et documents liés conservés. Nécessite opportunities.write.
        """
        grant_id, client = runtime.context(write=True)
        require_opportunity_scope(runtime, grant_id)
        live = await client.get(f"/opportunities/{params.opportunity_id}")
        if live.get("id") != params.opportunity_id:
            raise ToolError("invalid_response", "L'identité de l'opportunité n'est pas vérifiée.")
        before = opportunity_snapshot(live)
        payload = params.model_dump(mode="json", exclude_unset=True, exclude={"opportunity_id"})
        for key in ("pipeline", "step"):
            if f"{key}_id" in payload:
                payload[key] = payload.pop(f"{key}_id")
        expected = {**before, **payload}
        if not opportunity_mismatches(expected, live):
            raise ToolError("no_changes", "Aucune modification effective de l'opportunité.")
        # Sellsy documents defaults for status/owner: preserve them explicitly.
        for key in ("status", "owner_id"):
            if key in before:
                payload.setdefault(key, before[key])
        references = await validate_opportunity_update(client, params.opportunity_id, payload, expected)
        manifest = runtime.manifests.create(
            grant_id=grant_id,
            operation="opportunity.update",
            object_type="opportunity",
            object_id=str(params.opportunity_id),
            payload=payload,
            before_fingerprint=fingerprint(live),
            checks={"expected": expected, "before_sections": section_fingerprints(live)},
        )
        manifest["diff"] = {
            key: {"before": before.get(key), "after": value}
            for key, value in payload.items()
            if before.get(key) != value
        }
        manifest["resolved"] = references
        return manifest

    @runtime.tool(
        mcp,
        name="sellsy_apply_opportunity_update",
        title="Modifier l'opportunité approuvée",
        annotations=WRITE_HINTS,
    )
    async def apply_opportunity_update(params: ApplyManifest) -> dict[str, Any]:
        """Applique une fois le manifeste approuvé ; refuse un état changé et vérifie la relecture.
        En cas de résultat incertain, consulter sellsy_get_manifest_result ; ne pas rejouer.
        """
        return await apply_operation(runtime, params, "opportunity.update")

    @runtime.tool(
        mcp,
        name="sellsy_prepare_opportunity_create",
        title="Préparer une opportunité Sellsy",
        annotations=READ_HINTS,
    )
    async def prepare_opportunity_create(params: OpportunityCreate) -> dict[str, Any]:
        """Prépare une opportunité ouverte sans création Sellsy : client, contacts, pipeline/étape,
        doublons et validation API. Mettre le contexte et les liens Gmail/ticket dans note.
        Omettre montant et échéance s'ils sont inconnus. Nécessite opportunities.write.
        """
        grant_id, client = runtime.context(write=True)
        require_opportunity_scope(runtime, grant_id)
        payload = params.model_dump(mode="json", exclude_none=True)
        payload["related"] = [{"type": "company", "id": payload.pop("company_id")}]
        payload["pipeline"] = payload.pop("pipeline_id")
        payload["step"] = payload.pop("step_id")
        payload["status"] = "open"
        if "main_estimate_id" in payload:
            payload["main_doc_id"] = payload.pop("main_estimate_id")
        checks = await validate_opportunity_creation(client, payload)
        if payload.get("main_doc_id"):
            estimate = await client.get(f"/estimates/{payload['main_doc_id']}")
            if fingerprint(estimate) != checks["estimate_fingerprint"]:
                raise ToolError("stale_manifest", "Le devis lié a changé pendant la préparation.")
            checks["estimate_snapshot"] = snapshot(estimate)
        manifest = runtime.manifests.create(
            grant_id=grant_id,
            operation="opportunity.create",
            object_type="opportunity",
            object_id=None,
            payload=payload,
            before_fingerprint=None,
            checks=checks,
        )
        manifest["resolved"] = {
            k: v for k, v in checks.items() if k in {"company", "pipeline", "step", "duplicate_check"}
        }
        return manifest

    @runtime.tool(
        mcp,
        name="sellsy_apply_opportunity_create",
        title="Créer l'opportunité approuvée",
        annotations=WRITE_HINTS,
    )
    async def apply_opportunity_create(params: ApplyManifest) -> dict[str, Any]:
        """Crée une fois après approbation du manifeste, contrôle les doublons et compare le readback.
        En cas de résultat incertain, consulter sellsy_get_manifest_result ; ne pas rejouer.
        """
        return await apply_operation(runtime, params, "opportunity.create")

    @runtime.tool(
        mcp,
        name="sellsy_prepare_estimate_create",
        title="Préparer la création d’un devis",
        annotations=READ_HINTS,
    )
    async def prepare_estimate_create(params: EstimateCreate) -> dict[str, Any]:
        """Valide les rattachements et taxes ; prépare un brouillon sans écrire dans Sellsy."""
        grant_id, client = runtime.context(write=True)
        if (
            any(row.purchase_amount is not None for row in params.rows)
            and not params.include_costs_in_preview
        ):
            raise ValueError("Un coût modifié doit être visible dans l’aperçu approuvé.")
        rows = await new_estimate_rows(
            client, params.rows, catalog_types=("product", "service", "packaging", "shipping")
        )
        payload = {
            "related": [{"type": "company", "id": params.company_id}],
            "status": "draft",
            "subject": params.subject,
            "rows": rows,
        }
        for key in ("contact_id", "invoicing_address_id", "delivery_address_id"):
            if getattr(params, key) is not None:
                payload[key] = getattr(params, key)
        company = await validate_creation(client, payload)
        manifest = runtime.manifests.create(
            grant_id=grant_id,
            operation="estimate.create",
            object_type="estimate",
            object_id=None,
            payload=payload,
            before_fingerprint=None,
        )
        manifest["resolved_company"] = {"id": company["id"], "name": company.get("name")}
        return _without_costs(manifest, params.include_costs_in_preview)

    @runtime.tool(
        mcp,
        name="sellsy_prepare_estimate_update",
        title="Préparer la modification d’un devis",
        annotations=READ_HINTS,
    )
    async def prepare_estimate_update(params: EstimateUpdate) -> dict[str, Any]:
        """Prépare l’objet et les lignes d’un devis brouillon. rows modifie des lignes par ID,
        add_rows ajoute en fin de devis, remove_row_ids supprime par ID. Présenter le diff avant validation.
        """
        grant_id, client = runtime.context(write=True)
        if (
            any(row.purchase_amount is not None for row in [*params.rows, *params.add_rows])
            and not params.include_costs_in_preview
        ):
            raise ValueError("Un coût modifié doit être visible dans l’aperçu approuvé.")
        live = await client.get(f"/estimates/{params.estimate_id}")
        payload, expected, diff = await estimate_update_plan(client, live, params)
        manifest = runtime.manifests.create(
            grant_id=grant_id,
            operation="estimate.update",
            object_type="estimate",
            object_id=str(params.estimate_id),
            payload=payload,
            before_fingerprint=fingerprint(live),
            checks={"expected": expected, "before_sections": section_fingerprints(live)},
        )
        manifest["diff"] = diff
        return _without_costs(manifest, params.include_costs_in_preview)

    @runtime.tool(
        mcp,
        name="sellsy_prepare_estimate_status_update",
        title="Préparer un statut de devis",
        annotations=READ_HINTS,
    )
    async def prepare_estimate_status_update(params: EstimateStatusUpdate) -> dict[str, Any]:
        """Prépare une transition permise sans la déclencher ; pas de statut financier calculé imposé."""
        grant_id, client = runtime.context(write=True)
        live = await client.get(f"/estimates/{params.estimate_id}")
        validate_transition(live.get("status"), params.status)
        manifest = runtime.manifests.create(
            grant_id=grant_id,
            operation="estimate.status.update",
            object_type="estimate",
            object_id=str(params.estimate_id),
            payload={"status": params.status},
            before_fingerprint=fingerprint(live),
            checks={"before_sections": section_fingerprints(live)},
        )
        manifest["transition"] = {"before": live.get("status"), "after": params.status}
        return manifest

    @runtime.tool(
        mcp, name="sellsy_apply_estimate_create", title="Créer le devis approuvé", annotations=WRITE_HINTS
    )
    async def apply_estimate_create(params: ApplyManifest) -> dict[str, Any]:
        """Applique une fois après approbation explicite ; compare le client et les lignes au manifeste."""
        return await apply_operation(runtime, params, "estimate.create")

    @runtime.tool(
        mcp, name="sellsy_apply_estimate_update", title="Modifier le devis approuvé", annotations=WRITE_HINTS
    )
    async def apply_estimate_update(params: ApplyManifest) -> dict[str, Any]:
        """Applique une fois après approbation explicite ; vérifie champs modifiés et lignes conservées."""
        return await apply_operation(runtime, params, "estimate.update")

    @runtime.tool(
        mcp,
        name="sellsy_apply_estimate_status_update",
        title="Appliquer le statut approuvé",
        annotations=WRITE_HINTS,
    )
    async def apply_estimate_status_update(params: ApplyManifest) -> dict[str, Any]:
        """Applique une transition approuvée une seule fois et confirme le statut relu."""
        return await apply_operation(runtime, params, "estimate.status.update")

    @runtime.tool(
        mcp,
        name="sellsy_get_manifest_result",
        title="Vérifier une opération préparée",
        annotations=READ_HINTS,
    )
    async def get_manifest_result(params: ManifestStatus) -> dict[str, Any]:
        """Retourne le résultat persistant d’une écriture pour cet utilisateur, sans la rejouer."""
        grant_id, _ = runtime.context()
        return runtime.manifests.read_outcome(params.manifest_id, grant_id)
