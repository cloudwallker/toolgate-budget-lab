"""Catch identity injection, invalid arguments and unsafe evidence acceptance."""

import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from toolgate.contracts import CallRequest, ToolEvidence, fingerprint


@pytest.mark.parametrize(
    "payload",
    [
        {"tool": "catalog.lookup", "arguments": {"sku": "A"}, "actor": "other"},
        {"tool": "catalog.lookup", "arguments": {"sku": "A", "url": "http://elsewhere"}},
        {"tool": "shell.exec", "arguments": {"sku": "A"}},
        {"tool": "catalog.lookup", "arguments": {"sku": "   "}},
        {"tool": "catalog.lookup", "arguments": {"sku": 123}},
    ],
)
def test_rejects_identity_injection_and_invalid_tool_arguments(payload):
    with pytest.raises(ValidationError):
        CallRequest.model_validate(payload)


def test_same_semantic_request_has_same_fingerprint():
    left = CallRequest.model_validate_json('{"tool":"catalog.lookup","arguments":{"sku":"A"}}')
    right = CallRequest.model_validate_json('{"arguments":{"sku":"A"},"tool":"catalog.lookup"}')
    assert fingerprint(left) == fingerprint(right)
    changed = CallRequest(tool="catalog.lookup", arguments={"sku": "B"})
    assert fingerprint(left) != fingerprint(changed)
    assert len(fingerprint(left)) == 64


def test_running_evidence_cannot_claim_result_or_release_slot():
    with pytest.raises(ValidationError):
        ToolEvidence(operation_id=UUID(int=1), state="running", result={"price": 100})
    evidence = ToolEvidence(operation_id=UUID(int=1), state="running", result=None)
    assert not evidence.is_terminal


def test_terminal_evidence_requires_matching_operation_and_result():
    evidence = ToolEvidence(operation_id=UUID(int=1), state="succeeded", result={"price": 100})
    assert evidence.is_terminal
    assert evidence.matches(UUID(int=1))
    assert not evidence.matches(UUID(int=2))
    with pytest.raises(ValidationError):
        ToolEvidence(operation_id=UUID(int=1), state="succeeded", result=None)
    with pytest.raises(ValidationError):
        ToolEvidence.model_validate(
            json.loads(
                '{"operation_id":"00000000-0000-0000-0000-000000000001",'
                '"state":"unknown","result":null}'
            )
        )


def test_failed_tool_evidence_without_result_is_terminal():
    evidence = ToolEvidence(operation_id=UUID(int=1), state="failed", result=None)
    assert evidence.is_terminal and evidence.matches(UUID(int=1))
