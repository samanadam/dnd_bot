"""Cloudflare R2 as the handover medium.

R2 replaces the shared filesystem between the recorder and the transcriber.
Neither machine needs to reach the other any more: both talk outbound to
Cloudflare, which is what lets the transcriber be a laptop behind a home router
in a different country to the VPS.

The object layout mirrors the directory layout in `contract.py` exactly:

    outbox/<session_id>/metadata.json
    outbox/<session_id>/<user_id>.opus
    outbox/<session_id>/READY          <- zero bytes, written last
    inbox/<session_id>/transcript.md
    inbox/<session_id>/DONE            <- zero bytes, written last

Because the marker is still the last write, a session mid-upload is still
invisible to the other side. That property is the whole reason the handover is
safe to interrupt, and it survives the move to object storage unchanged - which
is why `contract.py` itself needs no edits at all.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

OUTBOX_PREFIX = "outbox"
INBOX_PREFIX = "inbox"


class R2Error(RuntimeError):
    """Raised when object storage cannot be reached or used."""


def session_prefix(prefix: str, session_id: str) -> str:
    """Key prefix for one session, with the id validated as a single segment.

    Session ids come from our own database, but they also arrive as the names of
    things the *transcriber* created. Refusing separators here means a hostile
    or merely broken id can never reach outside its own prefix, nor - once it is
    used as a directory name on the way back in - outside the inbox.
    """
    session_id = str(session_id)
    if not session_id or "/" in session_id or "\\" in session_id or session_id in {".", ".."}:
        raise ValueError(f"Not a usable session id: {session_id!r}")
    return f"{prefix}/{session_id}/"


def build_client(config) -> Any:  # noqa: ANN001 - Config, avoiding an import cycle
    """An S3 client pointed at the account's R2 endpoint.

    boto3 is imported here rather than at module scope so that a deployment
    running the local-filesystem backend never needs it installed.
    """
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise R2Error(
            "STORAGE_BACKEND=r2 needs boto3. Rebuild the image, or pip install boto3."
        ) from exc

    return boto3.client(
        "s3",
        endpoint_url=f"https://{config.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=config.r2_access_key_id,
        aws_secret_access_key=config.r2_secret_access_key,
        # R2 ignores regions but the SigV4 signer insists on one.
        region_name="auto",
        config=BotoConfig(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


class R2Store:
    """Whole-session operations against one bucket.

    Deliberately session-shaped rather than object-shaped: every caller wants
    "put this session", "list what is finished", "take this session away", and
    keeping the marker-last ordering inside those operations means no caller can
    forget it.
    """

    def __init__(self, client: Any, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    @classmethod
    def from_config(cls, config) -> R2Store:  # noqa: ANN001 - Config
        return cls(build_client(config), config.r2_bucket)

    # -- primitives --------------------------------------------------------

    def put_file(self, key: str, path: Path) -> None:
        self.client.upload_file(Filename=str(path), Bucket=self.bucket, Key=key)

    def put_bytes(self, key: str, body: bytes = b"") -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body)

    def list_keys(self, prefix: str) -> list[str]:
        """Every key under a prefix, following continuation tokens."""
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = self.client.list_objects_v2(**kwargs)
            keys += [item["Key"] for item in response.get("Contents", [])]
            token = response.get("NextContinuationToken")
            if not response.get("IsTruncated") or not token:
                break
        return sorted(keys)

    def has_object(self, key: str) -> bool:
        # A prefix listing rather than head_object: one code path, and no
        # botocore exception types to catch (or to fake in tests).
        return key in self.list_keys(key)

    # -- session operations ------------------------------------------------

    def upload_session(self, prefix: str, session_id: str, directory: Path, *, marker: str) -> int:
        """Upload a staged session directory. Returns the file count, marker aside.

        The marker is written last and separately, so an upload interrupted
        halfway leaves a session the other side simply does not see yet.
        """
        base = session_prefix(prefix, session_id)
        directory = Path(directory)
        files = sorted(p for p in directory.iterdir() if p.is_file() and p.name != marker)
        for path in files:
            self.put_file(f"{base}{path.name}", path)
        self.put_bytes(f"{base}{marker}")
        return len(files)

    def download_session(self, prefix: str, session_id: str, destination: Path) -> Path:
        """Pull a whole session down into `destination/<session_id>/`."""
        base = session_prefix(prefix, session_id)
        target = Path(destination) / session_id
        target.mkdir(parents=True, exist_ok=True)
        for key in self.list_keys(base):
            name = key[len(base) :]
            if not name or "/" in name:  # no nested keys in this layout
                continue
            self.client.download_file(Bucket=self.bucket, Key=key, Filename=str(target / name))
        return target

    def delete_session(self, prefix: str, session_id: str) -> int:
        """Remove every object for a session. Returns how many were deleted."""
        base = session_prefix(prefix, session_id)
        keys = self.list_keys(base)
        for key in keys:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        return len(keys)

    def marked_sessions(self, prefix: str, marker: str) -> list[str]:
        """Session ids whose marker object exists - i.e. finished uploading."""
        suffix = f"/{marker}"
        found = []
        for key in self.list_keys(f"{prefix}/"):
            if not key.endswith(suffix):
                continue
            session_id = key[len(prefix) + 1 : -len(suffix)]
            if session_id and "/" not in session_id:
                found.append(session_id)
        return sorted(found)
