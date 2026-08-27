#!/usr/bin/env python3
"""Container healthcheck: the bot's main loop must have touched the heartbeat file recently."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

MAX_AGE_SECONDS = int(os.environ.get("HEARTBEAT_MAX_AGE_SECONDS", "120"))
HEARTBEAT = Path(os.environ.get("DATA_DIR", "/data")) / "heartbeat"


def main() -> int:
    if not HEARTBEAT.exists():
        print(f"heartbeat missing: {HEARTBEAT}", file=sys.stderr)
        return 1
    age = time.time() - HEARTBEAT.stat().st_mtime
    if age > MAX_AGE_SECONDS:
        print(f"heartbeat stale: {age:.0f}s old", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
