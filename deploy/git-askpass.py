#!/usr/bin/env python3
"""Fixed Git credential helper. Only the worker mounts the repository token."""

import os
from pathlib import Path
import sys


def main() -> int:
    prompt = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if "username" in prompt:
        print("x-access-token")
        return 0
    if "password" in prompt:
        try:
            value = Path(os.environ["DMASIF_REPOSITORY_TOKEN_FILE"]).read_text().strip()
        except (KeyError, OSError):
            return 1
        if not value or "\n" in value or "\r" in value:
            return 1
        print(value)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
