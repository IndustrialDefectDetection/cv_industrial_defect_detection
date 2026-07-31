"""Load the API token from a container secret before starting the server."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,512}")


def main() -> None:
    token = os.getenv("MES_INTERNAL_API_TOKEN", "")
    token_file = os.getenv("MES_INTERNAL_API_TOKEN_FILE", "").strip()

    if not token and token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8")
        except OSError:
            sys.exit("Unable to read the MES internal API token secret")

    if _TOKEN_PATTERN.fullmatch(token) is None:
        sys.exit(
            "MES_INTERNAL_API_TOKEN must be 32-512 characters using only "
            "letters, numbers, underscores, or hyphens"
        )
    if len(sys.argv) < 2:
        sys.exit("No server command was provided")

    os.environ["MES_INTERNAL_API_TOKEN"] = token
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
