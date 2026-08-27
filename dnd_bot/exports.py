"""Session export bundles.

Exports live in /data/exports and are deliberately exempt from the audio
retention cleanup - they are the escape hatch for keeping audio past the
retention window.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from . import paths
from .chunking import CHUNK_DIR_NAME

log = logging.getLogger(__name__)


class ExportError(RuntimeError):
    """Raised when there is nothing to export."""


def build_export(sessions_root: Path, exports_root: Path, session_id: str) -> Path:
    session_dir = paths.session_dir(sessions_root, session_id)
    if not session_dir.is_dir():
        raise ExportError(f"No files on disk for session `{session_id}`.")

    # Skip temporary transcription chunks - they are duplicates of the tracks.
    files = [
        p
        for p in session_dir.rglob("*")
        if p.is_file() and CHUNK_DIR_NAME not in p.relative_to(session_dir).parts
    ]
    if not files:
        raise ExportError(f"Session `{session_id}` has no files left to export.")

    exports_root.mkdir(parents=True, exist_ok=True)
    target = paths.export_path(exports_root, session_id)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, arcname=str(path.relative_to(session_dir)))
    log.info("Exported session %s to %s (%.1f MB)", session_id, target, size_mb(target))
    return target


def size_mb(path: Path) -> float:
    return path.stat().st_size / 1_000_000


def fits_discord_upload(path: Path, limit_mb: int) -> bool:
    return size_mb(path) <= limit_mb
