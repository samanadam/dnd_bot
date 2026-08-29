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

log = logging.getLogger(__name__)


class ExportError(RuntimeError):
    """Raised when there is nothing to export."""


def build_export(
    sessions_root: Path,
    exports_root: Path,
    session_id: str,
    outbox_root: Path | None = None,
) -> Path:
    """Bundle a session's transcript and whatever audio is still here.

    Audio moves to the outbox when a session is staged, and disappears entirely
    once the transcriber confirms it has a copy - so an export is transcript
    plus audio before collection, and transcript alone afterwards.
    """
    session_dir = paths.session_dir(sessions_root, session_id)
    sources: list[tuple[Path, Path]] = []
    if session_dir.is_dir():
        sources += [(p, session_dir) for p in session_dir.rglob("*") if p.is_file()]
    if outbox_root is not None:
        staged = Path(outbox_root) / session_id
        if staged.is_dir():
            sources += [(p, staged) for p in staged.rglob("*") if p.is_file()]

    if not sources:
        raise ExportError(f"Session `{session_id}` has no files left to export.")

    exports_root.mkdir(parents=True, exist_ok=True)
    target = paths.export_path(exports_root, session_id)
    seen: set[str] = set()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, root in sorted(sources):
            arcname = str(path.relative_to(root))
            if arcname in seen:
                continue
            seen.add(arcname)
            archive.write(path, arcname=arcname)
    log.info("Exported session %s to %s (%.1f MB)", session_id, target, size_mb(target))
    return target


def size_mb(path: Path) -> float:
    return path.stat().st_size / 1_000_000


def fits_discord_upload(path: Path, limit_mb: int) -> bool:
    return size_mb(path) <= limit_mb
