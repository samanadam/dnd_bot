#!/usr/bin/env python3
"""Exit 0 when a recording is active (or the answer is unknown), 1 when idle.

Used by the host autoheal script: it must never restart a bot that may be
recording, so anything other than a clear "nothing is recording" counts as busy.
Exit 2 means the API is turned off, which autoheal also treats as busy.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    if os.environ.get("API_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return 2
    port = os.environ.get("API_PORT", "8080")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/recording",
        headers={"Authorization": f"Bearer {os.environ.get('API_TOKEN', '')}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - loopback only
            active = json.load(response)
    except Exception:  # noqa: BLE001 - unknown state is treated as recording
        return 0
    return 0 if active else 1


if __name__ == "__main__":
    sys.exit(main())
