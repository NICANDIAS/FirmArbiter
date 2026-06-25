#!/usr/bin/env python3

from __future__ import annotations

import sys


def main() -> int:
    print(
        "The FirmAE source-pinned bootstrap image is not yet "
        "a runnable VERITAS adapter.",
        file=sys.stderr,
    )

    print(
        "Dependency installation, lifecycle translation and "
        "candidate execution will be added in later checkpoints.",
        file=sys.stderr,
    )

    return 78


if __name__ == "__main__":
    raise SystemExit(main())
