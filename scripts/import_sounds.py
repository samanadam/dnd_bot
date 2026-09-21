#!/usr/bin/env python
"""Put a local folder of ambience and effects into the music bucket.

The library lives in R2; this is only the way in. It reads the folder once and
keeps no copy. Lay the folder out like this:

    sounds/
        ambience/   tavern rain.ogg, forest night.mp3, ...
        sfx/        sword hit.wav, door slam.ogg, ...

Then check what would happen, and do it:

    python scripts/import_sounds.py ./sounds --dry-run
    python scripts/import_sounds.py ./sounds

Every file goes through the same checks as a portal upload. Names already in the
bucket are skipped, never overwritten, so running it twice is safe.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dnd_bot.config import ConfigError, load_config  # noqa: E402
from dnd_bot.r2 import R2Error, R2Store  # noqa: E402
from dnd_bot.soundimport import NothingToImport, import_sounds  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import ambience and effects into R2.")
    parser.add_argument("folder", type=Path, help="folder holding ambience/ and/or sfx/")
    parser.add_argument("--dry-run", action="store_true", help="check everything, upload nothing")
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2
    if not config.uses_r2:
        print("STORAGE_BACKEND is not 'r2', so there is no bucket to import into.")
        return 2

    try:
        store = R2Store.from_config(config)
        outcomes = import_sounds(
            args.folder,
            store,
            prefix=config.music_r2_prefix,
            max_bytes=config.music_upload_max_mb * 1_000_000,
            max_ambience_seconds=config.music_max_track_seconds,
            dry_run=args.dry_run,
        )
    except NothingToImport as exc:
        print(exc)
        return 2
    except R2Error as exc:
        print(f"Could not reach the bucket: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - a listing failure is the likely one
        print(f"Import stopped: {type(exc).__name__}: {exc}")
        return 1

    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
        detail = f" - {outcome.reason}" if outcome.reason else ""
        print(f"  {outcome.status:<12} {outcome.folder}/{outcome.path.name}{detail}")

    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    print(f"\n{summary or 'No sound files found.'}")
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
