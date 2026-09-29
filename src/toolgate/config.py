"""Explicit local configuration with no import-time side effects."""

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in principal configuration")
        result[key] = value
    return result


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    tool_base_url: str
    tool_internal_key: str = field(repr=False)
    principal_keys: tuple[tuple[bytes, str], ...] = field(repr=False)
    total_timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, environment=None):
        values = os.environ if environment is None else environment
        required = ["DATABASE_URL", "TOOL_BASE_URL", "TOOL_INTERNAL_KEY", "PRINCIPAL_KEYS"]
        if any(not values.get(name) for name in required):
            raise ValueError("missing required ToolGate configuration")
        database_url = values["DATABASE_URL"]
        if not database_url.startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL requires PostgreSQL psycopg")
        base_url = values["TOOL_BASE_URL"].rstrip("/")
        url = urlsplit(base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("invalid fixed tool base URL")
        try:
            keys = json.loads(values["PRINCIPAL_KEYS"], object_pairs_hook=unique_pairs)
        except (ValueError, TypeError):
            raise ValueError("invalid principal key configuration") from None
        if not isinstance(keys, dict) or not keys:
            raise ValueError("at least one principal key is required")
        digests = []
        for key, principal in keys.items():
            if (
                not isinstance(key, str)
                or not key
                or not isinstance(principal, str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", principal)
            ):
                raise ValueError("invalid principal key configuration")
            digests.append((hashlib.sha256(key.encode()).digest(), principal))
        timeout = float(values.get("TOTAL_TIMEOUT_SECONDS", "10"))
        if not 0.05 <= timeout <= 60:
            raise ValueError("total timeout must be between 0.05 and 60 seconds")
        return cls(database_url, base_url, values["TOOL_INTERNAL_KEY"], tuple(digests), timeout)

    def principal_for_key(self, key: str) -> str | None:
        supplied = hashlib.sha256(key.encode()).digest()
        matched = None
        for expected, principal in self.principal_keys:
            if hmac.compare_digest(expected, supplied):
                matched = principal
        return matched
