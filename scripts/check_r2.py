#!/usr/bin/env python
"""Verify the R2 credentials before trusting a session to them.

Writes a small object, reads it back, lists it and deletes it - the same four
operations the handover uses. Run it once after filling in `.env`, rather than
discovering a typo at the end of a four-hour game:

    python scripts/check_r2.py
    docker compose run --rm dnd-bot python scripts/check_r2.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dnd_bot.config import ConfigError, load_config  # noqa: E402
from dnd_bot.r2 import R2Error, R2Store  # noqa: E402

PROBE_KEY = "diagnostics/connectivity-probe"


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2

    if not config.uses_r2:
        print("STORAGE_BACKEND is not 'r2', so there is nothing to check.")
        return 0

    print(f"Bucket {config.r2_bucket} on account {config.r2_account_id[:8]}...")
    try:
        store = R2Store.from_config(config)
    except R2Error as exc:
        print(f"Could not build the client: {exc}")
        return 1

    try:
        store.put_bytes(PROBE_KEY, b"dnd-bot connectivity probe\n")
        print("  write   ok")

        if not store.has_object(PROBE_KEY):
            print("  list    FAILED - the object was written but does not list")
            return 1
        print("  list    ok")

        store.client.delete_object(Bucket=store.bucket, Key=PROBE_KEY)
        print("  delete  ok")
    except Exception as exc:  # noqa: BLE001 - this script exists to report failures
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        print(
            "\nCheck that the API token has 'Object Read & Write' on this bucket, "
            "that R2_ACCOUNT_ID is the account id from the R2 overview page (not "
            "the token id), and that R2_BUCKET names an existing bucket."
        )
        return 1

    print("\nR2 is reachable and writable. Both halves must use this same bucket.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
