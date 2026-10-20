"""Catch invalid configuration and accidental disclosure of credentials."""

import pytest

from toolgate.config import Settings


def environment():
    return {
        "DATABASE_URL": "postgresql+psycopg://gateway:example@localhost/toolgate",
        "TOOL_BASE_URL": "http://127.0.0.1:8001",
        "TOOL_INTERNAL_KEY": "fixture-internal",
        "PRINCIPAL_KEYS": '{"fixture-alpha":"alpha","fixture-beta":"beta"}',
    }


def test_identity_is_derived_from_configured_key_and_never_from_request():
    settings = Settings.from_env(environment())
    assert settings.principal_for_key("fixture-alpha") == "alpha"
    assert settings.principal_for_key("fixture-beta") == "beta"
    assert settings.principal_for_key("wrong") is None
    assert "fixture-alpha" not in repr(settings)
    assert "fixture-internal" not in repr(settings)
    assert "example@" not in repr(settings)


def test_empty_or_ambiguous_key_configuration_fails_closed():
    for keys in ["{}", '{"":"alpha"}', '{"same":"alpha","same":"beta"}']:
        values = {**environment(), "PRINCIPAL_KEYS": keys}
        with pytest.raises(ValueError):
            Settings.from_env(values)


def test_non_postgres_database_and_unsafe_base_url_are_rejected():
    with pytest.raises(ValueError):
        Settings.from_env({**environment(), "DATABASE_URL": "sqlite:///local.db"})
    for url in ["file:///etc", "http://user:pass@host", "http://host/path?token=secret"]:
        with pytest.raises(ValueError):
            Settings.from_env({**environment(), "TOOL_BASE_URL": url})


@pytest.mark.parametrize("port", ["not-a-port", "65536", "-1", "0"])
def test_invalid_tool_port_is_rejected_before_dispatch(port):
    with pytest.raises(ValueError, match="invalid fixed tool base URL"):
        Settings.from_env({**environment(), "TOOL_BASE_URL": f"http://localhost:{port}"})


@pytest.mark.parametrize(
    "url",
    ["http://localhost", "http://localhost:80", "http://localhost:65535", "http://[::1]:8001"],
)
def test_valid_tool_ports_preserve_the_configured_destination(url):
    assert Settings.from_env({**environment(), "TOOL_BASE_URL": url}).tool_base_url == url
