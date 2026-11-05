from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable
from typing import Any

from .errors import ToolError


def normalized(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return " ".join("".join(c for c in value if not unicodedata.combining(c)).split())


def text_matches(needle: str, values: Any) -> bool:
    tokens = re.findall(r"\w+", normalized(needle))
    haystack = normalized(values)
    return bool(tokens) and all(token in haystack for token in tokens)


def unwrap_data(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise RuntimeError("Sellsy response must contain a list of objects in data")
    return data


def following_offset(current: int | str, returned: Any, count: int) -> int | str | None:
    # Numeric offsets echo the current page. Opaque strings are continuation cursors.
    if isinstance(returned, int) and not isinstance(returned, bool):
        if returned < 0:
            raise RuntimeError("Negative Sellsy pagination offset")
        return returned + count if str(returned) == str(current) else returned
    if isinstance(returned, str) and returned:
        return returned
    if returned is None or returned == "":
        return current + count if isinstance(current, int) else None
    raise RuntimeError("Invalid Sellsy pagination offset")


async def bounded_search(
    client: Any,
    path: str,
    *,
    filters: dict[str, Any] | None = None,
    needle: str | None = None,
    fields: tuple[str, ...] = (),
    matcher: Callable[[dict[str, Any]], bool] | None = None,
    limit: int = 20,
    max_pages: int | None = None,
    cursor: str | None = None,
    method: str = "POST",
    query_params: dict[str, Any] | None = None,
    matcher_key: Any = None,
) -> dict[str, Any]:
    max_pages = client.settings.search_max_pages if max_pages is None else max_pages
    if not 1 <= limit <= 5000 or not 1 <= max_pages <= 100:
        raise ValueError("limit and max_pages must be positive and bounded")
    if matcher is not None and matcher_key is None:
        raise ValueError("A resumable matcher requires a stable matcher_key")
    if path in {
        f"/{f}/search"
        for f in (
            "companies",
            "contacts",
            "items",
            "estimates",
            "invoices",
            "credit-notes",
            "orders",
            "deliveries",
        )
    }:
        query_params = {"order": "id", "direction": "asc", **(query_params or {})}
    query = {
        "path": path,
        "method": method,
        "filters": filters,
        "needle": needle,
        "fields": fields,
        "params": query_params,
        "matcher": matcher_key,
    }
    state = (
        client.continuations.load(cursor, client.grant_id, query)
        if cursor
        else {
            "offset": 0,
            "inspected": 0,
            "pages": 0,
            "total": None,
            "pending": [],
            "seen_ids": [],
            "seen_offsets": [],
            "complete": False,
            "halted": None,
        }
    )
    matches = state.pop("pending")
    seen_ids = set(state["seen_ids"])
    seen_offsets = set(state["seen_offsets"])
    page_size = client.settings.search_page_size
    stop_reason = "complete" if state["complete"] else (state.get("halted") or "max_pages")
    deadline = time.monotonic() + client.settings.search_timeout_seconds
    for _ in range(max_pages):
        if state["complete"] or state.get("halted") or len(matches) >= limit:
            break
        if time.monotonic() >= deadline:
            stop_reason = "time_budget"
            break
        offset = state["offset"]
        if str(offset) in seen_offsets:
            stop_reason = "repeated_cursor"
            state["halted"] = stop_reason
            break
        seen_offsets.add(str(offset))
        payload = await client.request(
            method,
            path,
            params={**(query_params or {}), "limit": page_size, "offset": offset},
            json={"filters": filters or {}} if method == "POST" else None,
        )
        rows = unwrap_data(payload)
        pagination = payload.get("pagination") or payload.get("meta") or {}
        if not isinstance(pagination, dict):
            raise RuntimeError("Invalid Sellsy pagination metadata")
        total = pagination.get("total", payload.get("total"))
        if total is not None:
            if isinstance(total, bool) or int(total) < 0:
                raise RuntimeError("Invalid Sellsy total")
            state["total"] = int(total)
        state["pages"] += 1
        unique = 0
        for row in rows:
            key = str(row.get("id")) if row.get("id") is not None else None
            if key is not None and key in seen_ids:
                continue
            if key is not None:
                seen_ids.add(key)
            unique += 1
            state["inspected"] += 1
            if needle and not text_matches(needle, " ".join(str(row.get(f, "")) for f in fields)):
                continue
            if matcher and not matcher(row):
                continue
            matches.append(row)
        next_offset = following_offset(offset, pagination.get("offset"), len(rows))
        total = state["total"]
        state["complete"] = (
            state["inspected"] >= total
            if total is not None
            else len(rows) < page_size and not isinstance(pagination.get("offset"), str)
        )
        if state["complete"]:
            stop_reason = "complete"
            break
        if not rows or not unique or next_offset is None or str(next_offset) in seen_offsets:
            stop_reason = "empty_page" if not rows else "pagination_stalled"
            state["offset"] = next_offset
            state["halted"] = stop_reason
            break
        state["offset"] = next_offset
        if len(matches) >= limit:
            stop_reason = "result_limit"
            break
    pending = matches[limit:]
    truncated = not state["complete"] or bool(pending)
    resumable = bool(pending) or (
        not state["complete"]
        and stop_reason
        not in {
            "empty_page",
            "pagination_stalled",
            "repeated_cursor",
        }
    )
    state.update(pending=pending, seen_ids=list(seen_ids), seen_offsets=list(seen_offsets))
    next_cursor = client.continuations.save(client.grant_id, query, state) if resumable else None
    result = {
        "data": matches[:limit],
        "returned": min(len(matches), limit),
        "next_cursor": next_cursor,
        "stop_reason": "result_limit" if pending else stop_reason,
        "coverage": {
            "inspected": state["inspected"],
            "pages": state["pages"],
            "known_total": state["total"],
            "complete": state["complete"],
            "truncated": truncated,
        },
        "status": "incomplete" if truncated else ("found" if matches else "not_found"),
        "source": "filtered_api",
    }
    if truncated:
        result["next_action"] = (
            "Continuer avec next_cursor et les mêmes filtres ; ne pas conclure à une absence."
            if next_cursor
            else "Parcours interrompu ; ne pas conclure à une absence."
        )
    return result


def require_complete(result: dict[str, Any], purpose: str) -> None:
    coverage = result.get("coverage", {})
    if not coverage.get("complete") or coverage.get("truncated"):
        raise ToolError(
            "incomplete",
            f"Recherche incomplète : {purpose}.",
            coverage=coverage,
            next_cursor=result.get("next_cursor"),
            next_action="Continuer la recherche.",
        )
