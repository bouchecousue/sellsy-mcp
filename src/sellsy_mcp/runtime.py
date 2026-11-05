from __future__ import annotations

import logging
import re
import time
from functools import wraps
from typing import Any, get_type_hints
from uuid import uuid4

from mcp.server.auth.middleware.auth_context import get_access_token

from .audit import AuditLog
from .errors import error_result
from .manifests import CHANGE_SECTIONS, ManifestStore
from .oauth_provider import SellsyOAuthProvider
from .sellsy_client import SellsyClient

READ_HINTS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
WRITE_HINTS = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}
LOGGER = logging.getLogger(__name__)


class Runtime:
    def __init__(self, provider: SellsyOAuthProvider, manifests: ManifestStore, audit: AuditLog):
        self.provider, self.manifests, self.audit = provider, manifests, audit
        self.metrics = provider.metrics

    def context(self, *, write: bool = False) -> tuple[str, SellsyClient]:
        access = get_access_token()
        grant_id = getattr(access, "grant_id", None)
        if not access or not grant_id:
            raise PermissionError("Authenticated Sellsy grant is required")
        if write and ("sellsy.write" not in access.scopes or not self.provider.settings.enable_writes):
            raise PermissionError("The MCP token does not include enabled sellsy.write")
        return grant_id, SellsyClient(self.provider.settings, self.provider, grant_id)

    def tool(self, mcp: Any, **options: Any):
        def decorate(fn):
            fn.__annotations__ = get_type_hints(fn)

            @wraps(fn)
            async def wrapped(*args, **kwargs):
                start = time.monotonic()
                request_id = uuid4().hex
                access = get_access_token()
                grant_id = getattr(access, "grant_id", None)
                identity = (
                    self.provider.store.one(
                        "SELECT staff_id FROM sellsy_grants WHERE grant_id=?", (grant_id,)
                    )
                    if grant_id
                    else None
                )
                try:
                    result = await fn(*args, **kwargs)
                except Exception as exc:
                    result = error_result(exc)
                result["request_id"] = request_id
                outcome = "error" if "error" in result or result.get("applied") is False else "success"
                self.metrics[f"{options['name']}:{outcome}"] += 1
                stop_reason = result.get("stop_reason")
                if stop_reason:
                    self.metrics[f"search:stop:{stop_reason}"] += 1
                coverage = result.get("coverage", {})
                self.metrics["search:pages"] += coverage.get("pages", 0)
                if coverage.get("truncated"):
                    self.metrics["search:truncated"] += 1
                sections = result.get("error", {}).get("changed_sections", [])
                safe_sections = (
                    ",".join(name for name in (*CHANGE_SECTIONS, "unavailable") if name in sections) or "none"
                )
                reason = result.get("error", {}).get("rate_limit_reason", "none")
                safe_reason = (
                    reason
                    if reason
                    in {"read_queue_full", "shared_cooldown", "read_budget_exhausted", "upstream_429"}
                    else "none"
                )
                LOGGER.info(
                    "request_id=%s tool=%s outcome=%s elapsed_ms=%d staff_id=%s error_code=%s http_status=%s "
                    "changed_sections=%s rate_limit_reason=%s",
                    request_id,
                    options["name"],
                    outcome,
                    (time.monotonic() - start) * 1000,
                    re.sub(r"[^0-9]", "", str((identity or {}).get("staff_id", ""))) or "unknown",
                    re.sub(r"[^a-zA-Z0-9_.-]", "", str(result.get("error", {}).get("code", "none")))[:80],
                    re.sub(r"[^0-9]", "", str(result.get("error", {}).get("http_status", "")))[:3] or "none",
                    safe_sections,
                    safe_reason,
                )
                return result

            return mcp.tool(**options)(wrapped)

        return decorate
