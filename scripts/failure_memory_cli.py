#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info < (3, 13):  # noqa: UP036 - package can be launched by an older host
    print(
        "Failure Memory requires Python 3.13 or newer; Python 3.14 is recommended.",
        file=sys.stderr,
    )
    raise SystemExit(78)

sys.dont_write_bytecode = True

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "src"))


def _main() -> int:
    try:
        from failure_memory.cli.main import main

        return main()
    except Exception:
        print("Failure-memory CLI could not start", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
