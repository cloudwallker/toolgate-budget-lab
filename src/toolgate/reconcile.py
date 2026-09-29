"""Controlled local recovery: query only; never redispatch an uncertain operation."""

import asyncio
from datetime import UTC, datetime

import httpx

from toolgate.execution import UntrustedEvidence, query_evidence


async def reconcile(store, client, base_url, key, call_id, total_timeout_seconds=10):
    call = await store.get_call(call_id)
    if call is None or not call.slot_held or call.state == "reserved":
        return call
    try:
        async with asyncio.timeout(total_timeout_seconds):
            evidence = await query_evidence(client, "GET", base_url, key, call_id)
        if evidence.is_terminal:
            await store.finalize(call_id, evidence)
    except (TimeoutError, httpx.HTTPError, UntrustedEvidence):
        # 404 cannot prove a delayed request will never arrive.
        pass
    return await store.get_call(call_id)


async def reap_reserved(store, call_id, *, now=None):
    call = await store.get_call(call_id)
    if call is None or call.state != "reserved":
        return False
    if call.deadline > (now or datetime.now(UTC)):
        return False
    # abort_reserved competes with dispatch under the store's principal lock and CAS.
    return await store.abort_reserved(call_id)
