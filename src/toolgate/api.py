"""Fixed-tool HTTP gateway. Configuration and resources are explicit factory inputs."""

from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from toolgate.budget import BudgetStore
from toolgate.config import Settings
from toolgate.contracts import CallRequest
from toolgate.db import Database
from toolgate.execution import execute_call


def call_view(call):
    return {
        "id": str(call.id),
        "tool": call.tool,
        "state": call.state,
        "slot_held": call.slot_held,
        "result": call.result,
        "query_url": f"/v1/calls/{call.id}",
    }


def create_app(settings, database=None, client=None):
    owns_database, owns_client = database is None, client is None
    database = database or Database(settings.database_url)
    client = client or httpx.AsyncClient(
        follow_redirects=False,
        timeout=settings.total_timeout_seconds,
        transport=httpx.AsyncHTTPTransport(retries=0),
    )

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if owns_client:
                await client.aclose()
            if owns_database:
                await database.close()

    app = FastAPI(title="ToolGate Budget Lab", lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.store = BudgetStore(database, timeout_seconds=settings.total_timeout_seconds)

    async def authorize(authorization: Annotated[str | None, Header()] = None):
        principal = None
        if authorization is not None and authorization.startswith("Bearer "):
            principal = app.state.settings.principal_for_key(authorization[7:])
        if principal is None:
            await app.state.store.audit_rejection(None, "AUTH_DENIED")
            raise HTTPException(401, "AUTH_DENIED")
        return principal

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error):
        # FastAPI's default validation response contains the original input.
        try:
            await app.state.store.audit_rejection(None, "INVALID_REQUEST")
        except (SQLAlchemyError, ConnectionError):
            return JSONResponse({"error": "PERSISTENCE_UNAVAILABLE"}, status_code=503)
        return JSONResponse({"error": "INVALID_REQUEST"}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error):
        return JSONResponse({"error": error.detail}, status_code=error.status_code)

    @app.exception_handler(SQLAlchemyError)
    @app.exception_handler(ConnectionError)
    async def database_error(request: Request, error):
        return JSONResponse({"error": "PERSISTENCE_UNAVAILABLE"}, status_code=503)

    @app.get("/health/live")
    @app.get("/health")
    async def live():
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready():
        async with database.sessions() as session:
            await session.execute(text("SELECT id FROM gateway.principals LIMIT 1"))
        return {"status": "ready"}

    @app.post("/v1/calls")
    async def create_call(
        request: CallRequest,
        request_key: Annotated[UUID, Header(alias="Idempotency-Key")],
        principal: Annotated[str, Depends(authorize)],
    ):
        store = app.state.store
        admission = await store.admit(principal, request_key, request)
        timing = {
            "Server-Timing": (
                f"admission;dur={admission.decision_ms:.3f}, "
                f"principal_lock;dur={admission.lock_ms:.3f}"
            )
        }
        if admission.error is not None:
            statuses = {"TOOL_DENIED": 403, "PRINCIPAL_DISABLED": 403, "IDEMPOTENCY_CONFLICT": 409}
            return JSONResponse(
                {"error": admission.error},
                status_code=statuses.get(admission.error, 429),
                headers=timing,
            )
        call = admission.call
        if admission.is_new:
            call = await execute_call(
                store,
                client,
                app.state.settings.tool_base_url,
                app.state.settings.tool_internal_key,
                call.id,
                request,
                app.state.settings.total_timeout_seconds,
            )
        return JSONResponse(
            call_view(call),
            status_code=202 if call.slot_held else 200,
            headers=timing,
        )

    @app.get("/v1/calls/{call_id}")
    async def get_call(call_id: UUID, principal: Annotated[str, Depends(authorize)]):
        call = await app.state.store.get_call(call_id, principal)
        if call is None:
            raise HTTPException(404, "CALL_NOT_FOUND")
        return call_view(call)

    @app.get("/v1/budget")
    async def budget(principal: Annotated[str, Depends(authorize)]):
        snapshot = await app.state.store.budget(principal)
        if snapshot is None:
            raise HTTPException(403, "PRINCIPAL_DISABLED")
        return snapshot

    return app


def create_app_from_env():
    return create_app(Settings.from_env())
