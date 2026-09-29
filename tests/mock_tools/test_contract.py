from uuid import uuid4

import httpx
import pytest


def test_catalog_tools_read_fixed_records():
    from mock_tools.catalog import execute

    lookup = execute("catalog.lookup", "SKU-001")
    assert lookup["sku"] == "SKU-001"
    assert lookup["name"] == "机械键盘"
    assert execute("catalog.summary", "SKU-001") == {
        "sku": "SKU-001",
        "summary": "机械键盘：有线键盘，适合日常办公。",
    }
    assert execute("catalog.lookup", "unknown") == {"sku": "unknown", "found": False}


@pytest.mark.asyncio
async def test_internal_auth_and_strict_payload_without_database_io():
    from mock_tools.api import create_app

    app = create_app("postgresql+psycopg://unused:unused@localhost/unused", "test-internal-key")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        path = f"/operations/{uuid4()}"
        for method in (client.get, client.post):
            response = await method(path)
            assert response.status_code == 401
        headers = {"Authorization": "Bearer test-internal-key"}
        for body in (
            {"tool": "shell.exec", "arguments": {"sku": "SKU-001"}},
            {"tool": "catalog.lookup", "arguments": {"sku": " "}},
            {"tool": "catalog.lookup", "arguments": {"sku": 1}},
            {"tool": "catalog.lookup", "arguments": {"sku": "SKU-001", "delay": 1}},
            {"tool": "catalog.lookup", "arguments": {"sku": "SKU-001"}, "actor": "x"},
        ):
            response = await client.post(path, json=body, headers=headers)
            assert response.status_code == 422
        assert (
            await client.post(
                "/operations/not-uuid",
                json={"tool": "catalog.lookup", "arguments": {"sku": "SKU-001"}},
                headers=headers,
            )
        ).status_code == 422
    await app.state.store.close()
