"""Generate local demo credentials once; never print their values."""

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / ".env"
    if target.exists():
        print("Existing .env retained.")
        return
    names = (
        "POSTGRES_PASSWORD",
        "GATEWAY_DB_PASSWORD",
        "TOOLS_DB_PASSWORD",
        "TOOL_INTERNAL_KEY",
        "ALPHA_KEY",
        "BETA_KEY",
    )
    content = "\n".join(f"{name}={secrets.token_hex(32)}" for name in names)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content + "\nPOSTGRES_PORT=55438\n")
    print("Created ignored local .env; values are not displayed.")


if __name__ == "__main__":
    main()
