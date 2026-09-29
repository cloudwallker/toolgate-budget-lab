"""One confirmed dispatch; external uncertainty always keeps its database reservation."""

import asyncio

import httpx
from pydantic import ValidationError

from toolgate.contracts import ToolEvidence


class UntrustedEvidence(Exception):
    pass


async def query_evidence(client, method, base_url, key, call_id, *, request=None):
    """Only a matching, schema-valid 200 is evidence; redirects are never followed."""
    async with client.stream(
        method,
        f"{base_url.rstrip('/')}/operations/{call_id}",
        headers={"Authorization": f"Bearer {key}"},
        json=request.model_dump(mode="json") if request is not None else None,
        follow_redirects=False,
    ) as response:
        if response.status_code != 200:
            raise UntrustedEvidence
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 262144:
                raise UntrustedEvidence
        try:
            evidence = ToolEvidence.model_validate_json(bytes(body))
        except ValidationError:
            raise UntrustedEvidence from None
        if not evidence.matches(call_id):
            raise UntrustedEvidence
        return evidence


async def execute_call(
    store,
    client,
    base_url,
    key,
    call_id,
    request,
    total_timeout_seconds=10,
):
    # This await must return a confirmed commit before any external request starts.
    # An exception here is intentionally not translated into a successful claim.
    if not await store.claim_dispatch(call_id):
        return await store.get_call(call_id)
    try:
        async with asyncio.timeout(total_timeout_seconds):
            evidence = await query_evidence(client, "POST", base_url, key, call_id, request=request)
        if evidence.is_terminal:
            await store.finalize(call_id, evidence)
        else:
            await store.mark_unknown(call_id, "NONTERMINAL_RESPONSE")
    except asyncio.CancelledError:
        try:
            await asyncio.shield(store.mark_unknown(call_id, "WAIT_CANCELLED"))
        except Exception:
            # A failed status write leaves the existing dispatching/running slot held.
            pass
        raise
    except (TimeoutError, httpx.HTTPError, UntrustedEvidence):
        await store.mark_unknown(call_id, "UPSTREAM_UNCERTAIN")
    return await store.get_call(call_id)
