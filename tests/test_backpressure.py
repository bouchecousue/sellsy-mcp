from __future__ import annotations

import asyncio
import time

import pytest
import respx
from httpx import Response

from sellsy_mcp.backpressure import ReadBackpressure
from sellsy_mcp.errors import SellsyAPIError, ToolError
from sellsy_mcp.sellsy_client import SellsyClient, retry_delay


async def test_cooldown_shared_across_clients_even_on_final_attempt(runtime_tools):
    runtime, client, _ = runtime_tools
    runtime.provider.settings.read_attempts = 1
    other = SellsyClient(runtime.provider.settings, runtime.provider, "alice")
    times = []

    def respond(request):
        times.append(time.monotonic())
        if len(times) == 1:
            return Response(429, headers={"Retry-After": "0.06"}, json={})
        return Response(200, json={"id": 1})

    with respx.mock as router:
        router.get("https://api.sellsy.com/v2/invoices/1").mock(side_effect=respond)
        with pytest.raises(ToolError, match="Sellsy limite"):
            await client.get("/invoices/1")
        await asyncio.gather(other.get("/invoices/1"), client.get("/invoices/1"))
    assert times[1] - times[0] >= 0.06
    assert times[2] - times[1] >= 0.24
    assert client.read_backpressure.pending == 0


async def test_cooldown_extension_and_cancelled_wait_release_queue():
    gate = ReadBackpressure(limit=2)
    gate.defer(0.04)
    deadline = time.monotonic() + 2

    async def wait():
        with gate.admission():
            await gate.wait(deadline)

    first = asyncio.create_task(wait())
    await asyncio.sleep(0.01)
    gate.defer(0.08)
    extended = gate.cooldown_until
    second = asyncio.create_task(wait())
    await asyncio.sleep(0)
    with pytest.raises(ToolError) as full:
        with gate.admission():
            pass
    assert full.value.details["rate_limit_reason"] == "read_queue_full"
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert time.monotonic() >= extended
    assert gate.pending == 0


async def test_long_retry_after_fails_budget_without_early_send(runtime_tools):
    runtime, client, _ = runtime_tools
    with respx.mock as router:
        route = router.get("https://api.sellsy.com/v2/invoices/1").respond(
            429, headers={"Retry-After": "120"}, json={}
        )
        with pytest.raises(ToolError) as limited:
            await client.get("/invoices/1")
        assert limited.value.details["retry_after_seconds"] >= 119
        with pytest.raises(ToolError) as queued:
            await client.get("/invoices/1")
        assert queued.value.details["rate_limit_reason"] == "shared_cooldown"
        assert route.call_count == 1
        assert client.read_backpressure.pending == 0


@pytest.mark.parametrize("method", ["put", "post"])
async def test_write_is_never_retried_or_queued_for_read_cooldown(runtime_tools, method):
    _, client, _ = runtime_tools
    client.read_backpressure.defer(120)
    with respx.mock as router:
        route = router.route(method=method.upper(), url="https://api.sellsy.com/v2/estimates/1").respond(
            429, headers={"Retry-After": "120"}, json={}
        )
        with pytest.raises(SellsyAPIError):
            await getattr(client, method)("/estimates/1", {})
        assert route.call_count == 1


async def test_search_post_uses_same_read_gate(runtime_tools):
    _, client, _ = runtime_tools
    client.read_backpressure.defer(120)
    with respx.mock(assert_all_called=False) as router:
        route = router.post("https://api.sellsy.com/v2/invoices/search").respond(200, json={})
        with pytest.raises(ToolError):
            await client.post("/invoices/search", {})
        assert route.call_count == 0


def test_retry_after_does_not_shorten_long_delays_or_accept_nonfinite():
    assert retry_delay("120", 0) == 120
    assert retry_delay("NaN", 1) == 2
    assert retry_delay("inf", 0) == 1
    assert retry_delay("Thu, 01 Jan 1970 00:00:00 GMT", 0) == 0
