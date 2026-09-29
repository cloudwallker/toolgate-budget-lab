"""Run tests against the isolated test database without exposing credentials."""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]


def test_environment():
    values = dict(
        line.split("=", 1)
        for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines()
        if "=" in line and not line.startswith("#")
    )
    environment = os.environ.copy()
    port = values.get("POSTGRES_PORT", "55438")
    for variable, role, password in (
        ("TOOLGATE_TEST_DATABASE_URL", "toolgate_gateway", "GATEWAY_DB_PASSWORD"),
        ("MOCK_TOOLS_TEST_DATABASE_URL", "toolgate_tools", "TOOLS_DB_PASSWORD"),
    ):
        environment[variable] = (
            f"postgresql+psycopg://{role}:{quote(values[password], safe='')}"
            f"@127.0.0.1:{port}/toolgate_test"
        )
    environment["PYTHONUTF8"] = "1"
    return environment


def main():
    arguments = sys.argv[1:] or ["-q"]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *arguments],
        cwd=ROOT,
        env=test_environment(),
        check=False,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
