from __future__ import annotations

import asyncio
import math
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .backpressure import ReadBackpressure
from .config import Settings
from .continuations import Continuations
from .crypto import TokenCipher
from .errors import SellsyAPIError, ToolError
from .oauth_provider import SellsyOAuthProvider
from .pagination import bounded_search, normalized, unwrap_data  # noqa: F401
from .store import Store


def retry_delay(value: str | None, attempt: int) -> float:
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                delay = (when - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError):
                delay = float("nan")
        if math.isfinite(delay) and delay >= 0:
            return delay
        if math.isfinite(delay) and delay < 0:
            return 0
    return min(2**attempt, 8)


class SellsyClient:
    def __init__(self, settings: Settings, provider: SellsyOAuthProvider, grant_id: str):
        self.settings, self.provider, self.grant_id = settings, provider, grant_id

    @property
    def read_backpressure(self) -> ReadBackpressure:
        if not hasattr(self.provider, "read_backpressure"):
            self.provider.read_backpressure = ReadBackpressure(self.settings.read_queue_limit)
        return self.provider.read_backpressure

    @property
    def continuations(self) -> Continuations:
        if not hasattr(self.provider, "continuations"):
            store = getattr(self.provider, "store", None) or Store(self.settings.database_path)
            cipher = getattr(self.provider, "cipher", None) or TokenCipher(self.settings.encryption_key_file)
            self.provider.continuations = Continuations(store, cipher)
        return self.provider.continuations

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        read_only = method.upper() == "GET" or (method.upper() == "POST" and path.endswith("/search"))
        if read_only:
            with self.read_backpressure.admission():
                async with asyncio.timeout(self.settings.search_timeout_seconds):
                    return await self._request(method, path, params=params, json=json)
        return await self._request(method, path, params=params, json=json)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/") or "://" in path or path.startswith("//"):
            raise ValueError("Only relative Sellsy API paths are allowed")
        method = method.upper()
        read_only = method == "GET" or (method == "POST" and path.endswith("/search"))
        token, _ = await self.provider.get_sellsy_access_token(self.grant_id)
        url = str(self.settings.sellsy_api_base_url).rstrip("/") + path
        attempts = self.settings.read_attempts if read_only else 1
        refreshed = False
        start = time.monotonic()
        for attempt in range(attempts):
            try:
                # Gate immediately before sending, after waiting for the HTTP concurrency slot.
                semaphore = getattr(self.provider, "request_semaphore", None)
                async with semaphore if semaphore is not None else nullcontext():
                    if read_only:
                        await self.read_backpressure.wait(start + self.settings.search_timeout_seconds)
                    if hasattr(self.provider, "http_client"):
                        response = await self.provider.http_client.request(
                            method,
                            url,
                            params=params,
                            json=json,
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                        )
                    else:  # Standalone providers used by offline tests.
                        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                            response = await client.request(
                                method,
                                url,
                                params=params,
                                json=json,
                                headers={"Authorization": f"Bearer {token}"},
                            )
                    if response.status_code == 429:
                        # Publish before releasing the slot; writes are still never retried.
                        self.read_backpressure.defer(
                            retry_delay(response.headers.get("Retry-After"), attempt)
                        )
            except (httpx.TimeoutException, httpx.NetworkError):
                if not read_only or attempt + 1 == attempts:
                    raise
                await asyncio.sleep(retry_delay(None, attempt))
                continue
            if hasattr(self.provider, "metrics"):
                self.provider.metrics[f"http:{response.status_code}"] += 1
                if attempt:
                    self.provider.metrics["http:retry"] += 1
            if response.status_code == 401 and read_only and not refreshed and attempt + 1 < attempts:
                token, _ = await self.provider.refresh_after_401(self.grant_id, token)
                refreshed = True
                continue
            if read_only and response.status_code == 429:
                delay = max(0, self.read_backpressure.cooldown_until - time.monotonic())
                if (
                    attempt + 1 < attempts
                    and time.monotonic() + delay < start + self.settings.search_timeout_seconds
                ):
                    continue
                raise ToolError(
                    "rate_limited",
                    "Sellsy limite les lectures ; attendre avant de reprendre.",
                    http_status=429,
                    rate_limit_reason="upstream_429",
                    retry_after_seconds=max(1, math.ceil(delay)),
                    next_action="Réduire les lectures parallèles et attendre le délai indiqué.",
                )
            if read_only and response.status_code in {500, 502, 503, 504} and attempt + 1 < attempts:
                delay = retry_delay(response.headers.get("Retry-After"), attempt)
                if delay <= 30 and time.monotonic() - start + delay < self.settings.search_timeout_seconds:
                    await asyncio.sleep(delay)
                    continue
            if not response.is_success:
                self._raise_api_error(response)
            try:
                payload = response.json() if response.content else {}
            except ValueError:
                raise SellsyAPIError(response.status_code, "Invalid JSON response") from None
            if not isinstance(payload, dict):
                raise SellsyAPIError(response.status_code, "Expected a JSON object")
            if payload.get("error"):
                self._raise_payload_error(response.status_code, payload)
            return payload
        raise RuntimeError("Read attempts exhausted")

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", path, json=body)

    async def put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("PUT", path, json=body)

    async def bounded_search(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return await bounded_search(self, path, **kwargs)

    async def list_all(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return await bounded_search(self, path, method="GET", **kwargs)

    async def native_search(
        self, text: str, types: list[str], *, limit: int = 100, archived: bool | None = None
    ) -> dict[str, Any]:
        if not text.strip() or not types or not 1 <= limit <= 100:
            raise ValueError("Native search requires text, types and a bounded limit")
        params: dict[str, Any] = {"q": text, "type[]": types, "limit": limit}
        if archived is not None:
            params["archived"] = archived
        try:
            payload = await self.get("/search", params=params)
        except SellsyAPIError as exc:
            if exc.status_code == 403:
                raise ToolError(
                    "permission_denied",
                    "La recherche native nécessite search.read.",
                    required_scope="search.read",
                    next_action="Reconnecter l'application Sellsy.",
                ) from exc
            raise
        rows = unwrap_data(payload)
        data, seen = [], set()
        for row in rows:
            obj = row.get("object")
            if (
                not isinstance(obj, dict)
                or not isinstance(obj.get("id"), int)
                or isinstance(obj["id"], bool)
                or obj["id"] < 1
            ):
                raise RuntimeError("Native search response omitted object.id")
            type_name = str(obj.get("type"))
            if not any(t == type_name or t == f"{type_name}.{obj.get('sub_type')}" for t in types):
                raise RuntimeError("Native search returned an unexpected object type")
            key = (type_name, obj["id"])
            if key in seen:
                continue
            seen.add(key)
            data.append({**row, "id": obj["id"], "type": obj.get("sub_type") or type_name})
        pagination = payload.get("pagination") or {}
        total = pagination.get("total")
        complete = isinstance(total, int) and len(data) >= total
        return {
            "data": data,
            "returned": len(data),
            "source": "native_search",
            "next_cursor": None,
            "status": "incomplete" if not complete else ("found" if data else "not_found"),
            "stop_reason": "complete" if complete else "native_result_limit",
            "coverage": {
                "inspected": len(rows),
                "pages": 1,
                "known_total": total,
                "complete": complete,
                "truncated": not complete,
            },
            "next_action": None if complete else "Préciser la recherche ou utiliser les filtres métier.",
        }

    @staticmethod
    def _raise_api_error(response: httpx.Response) -> None:
        try:
            payload = response.json()
        except ValueError:
            raise SellsyAPIError(response.status_code, "non-JSON error response") from None
        SellsyClient._raise_payload_error(response.status_code, payload)

    @staticmethod
    def _raise_payload_error(status_code: int, payload: Any) -> None:
        error = payload.get("error", payload) if isinstance(payload, dict) else {}
        error = error if isinstance(error, dict) else {}
        raise SellsyAPIError(status_code, "request failed", error.get("code"), error.get("context"))
