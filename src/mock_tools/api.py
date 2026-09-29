import hashlib
import json
import os
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from mock_tools.catalog import execute
from mock_tools.models import Operation
from mock_tools.store import OperationStore


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sku: Annotated[str, Field(min_length=1, max_length=256)]

    @field_validator("sku")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sku must not be blank")
        return value


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tool: Literal["catalog.lookup", "catalog.summary"]
    arguments: Arguments


class Evidence(BaseModel):
    operation_id: UUID
    state: Literal["running", "succeeded", "failed"]
    result: dict | None


Hook = Callable[[UUID, ToolRequest], Awaitable[None]]


def fingerprint(request: ToolRequest) -> str:
    canonical = json.dumps(request.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def evidence(operation: Operation) -> Evidence:
    return Evidence(
        operation_id=operation.operation_id, state=operation.state, result=operation.result
    )


def create_app(database_url: str, internal_key: str, *, hook: Hook | None = None) -> FastAPI:
    if not internal_key:
        raise ValueError("An internal bearer key is required")
    store = OperationStore(database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            await store.close()

    app = FastAPI(title="ToolGate mock catalog", lifespan=lifespan)
    app.state.store = store
    app.state.hook = hook

    @app.exception_handler(SQLAlchemyError)
    @app.exception_handler(ConnectionError)
    async def persistence_error(request, error):
        # Neither original database messages nor SQL parameters enter server logs.
        return JSONResponse({"error": "PERSISTENCE_UNAVAILABLE"}, status_code=503)

    async def authorize(authorization: Annotated[str | None, Header()] = None):
        expected = f"Bearer {internal_key}"
        if authorization is None or not secrets.compare_digest(
            authorization.encode(), expected.encode()
        ):
            raise HTTPException(status_code=401, detail="Invalid internal credentials")

    @app.get(
        "/operations/{operation_id}", response_model=Evidence, dependencies=[Depends(authorize)]
    )
    async def get_operation(operation_id: UUID):
        operation = await store.get(operation_id)
        if operation is None:
            raise HTTPException(status_code=404, detail="Operation not found")
        return evidence(operation)

    @app.post(
        "/operations/{operation_id}", response_model=Evidence, dependencies=[Depends(authorize)]
    )
    async def start_operation(operation_id: UUID, request: ToolRequest):
        digest = fingerprint(request)
        owns_execution = await store.start(operation_id, digest, request.tool)
        if not owns_execution:
            operation = await store.get(operation_id)
            if operation is None:
                raise HTTPException(status_code=409, detail="Operation evidence unavailable")
            if operation.fingerprint != digest:
                raise HTTPException(status_code=409, detail="Operation payload conflict")
            if operation.state == "running":
                raise HTTPException(status_code=409, detail="Operation already running")
            return evidence(operation)
        # No database transaction remains open during tool execution.
        # CancelledError / process death leaves the durable started record running.
        try:
            if app.state.hook is not None:
                await app.state.hook(operation_id, request)
            result = execute(request.tool, request.arguments.sku)
        except Exception:
            await store.finalize(operation_id, "failed", None)
        else:
            await store.finalize(operation_id, "succeeded", result)
        operation = await store.get(operation_id)
        if operation is None:
            raise HTTPException(status_code=500, detail="Operation evidence unavailable")
        return evidence(operation)

    return app


def create_app_from_env() -> FastAPI:
    """Uvicorn factory; initialize the tools schema before launching this service."""
    return create_app(os.environ["TOOLS_DATABASE_URL"], os.environ["TOOLS_INTERNAL_KEY"])
