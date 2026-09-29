import asyncio

import pytest


@pytest.hookimpl(optionalhook=True)
def pytest_asyncio_loop_factories(config, item):
    return {"selector": asyncio.SelectorEventLoop}
