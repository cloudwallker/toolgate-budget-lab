"""Strict fixed-tool inputs and evidence with explicit terminal semantics."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sku: StrictStr = Field(min_length=1, max_length=64)

    @field_validator("sku")
    @classmethod
    def nonempty_sku(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sku must contain a non-whitespace character")
        return value


class CallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tool: Literal["catalog.lookup", "catalog.summary"]
    arguments: Arguments


class ToolEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID
    state: Literal["running", "succeeded", "failed"]
    result: dict | None

    @model_validator(mode="after")
    def result_matches_state(self):
        if self.state == "running" and self.result is not None:
            raise ValueError("running operation cannot have a result")
        if self.state == "succeeded" and self.result is None:
            raise ValueError("successful operation must have a result")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.state in {"succeeded", "failed"}

    def matches(self, operation_id: UUID) -> bool:
        return self.operation_id == operation_id


def fingerprint(request: CallRequest) -> str:
    body = json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
