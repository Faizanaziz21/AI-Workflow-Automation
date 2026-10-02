"""File storage abstraction (local filesystem by default; S3/GCS-compatible interface)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.sdk import ConnectorError, FileStore
from app.core.config import get_settings
from app.db.models import StoredFile


class BlobStorage(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class LocalBlobStorage:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root) + os.sep):
            raise ValueError("Invalid storage key")
        return path

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)

        await asyncio.to_thread(_write)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))


_storage: BlobStorage | None = None


def get_blob_storage() -> BlobStorage:
    global _storage
    if _storage is None:
        _storage = LocalBlobStorage(get_settings().storage_dir)
    return _storage


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str:
    base = os.path.basename(name or "file")
    return (_SAFE_NAME.sub("_", base) or "file")[:200]


def file_ref(f: StoredFile) -> dict[str, Any]:
    return {
        "file_id": str(f.id),
        "filename": f.filename,
        "content_type": f.content_type,
        "size": f.size_bytes,
        "sha256": f.sha256,
    }


async def store_file(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    filename: str,
    content: bytes,
    content_type: str,
    source: str,
    created_by: uuid.UUID | None = None,
) -> StoredFile:
    if len(content) > get_settings().max_upload_bytes:
        raise ValueError("File exceeds maximum size")
    file_id = uuid.uuid4()
    key = f"{org_id}/{workspace_id}/{file_id}/{safe_filename(filename)}"
    await get_blob_storage().put(key, content)
    record = StoredFile(
        id=file_id,
        org_id=org_id,
        workspace_id=workspace_id,
        filename=safe_filename(filename),
        content_type=content_type or "application/octet-stream",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        storage_key=key,
        source=source,
        created_by=created_by,
    )
    session.add(record)
    await session.flush()
    return record


async def load_file(session: AsyncSession, org_id: uuid.UUID, file_id: str) -> tuple[StoredFile, bytes]:
    try:
        fid = uuid.UUID(str(file_id))
    except ValueError as exc:
        raise ConnectorError(f"Invalid file id {file_id!r}") from exc
    record = (
        await session.execute(select(StoredFile).where(StoredFile.id == fid, StoredFile.org_id == org_id))
    ).scalar_one_or_none()
    if record is None:
        raise ConnectorError(f"File {file_id} not found")
    return record, await get_blob_storage().get(record.storage_key)


class PlatformFileStore(FileStore):
    """FileStore bound to one tenant/workspace, handed to connectors and nodes."""

    def __init__(self, session_factory: Any, org_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        self._session_factory = session_factory
        self.org_id = org_id
        self.workspace_id = workspace_id

    async def save(self, filename: str, content: bytes, content_type: str, source: str) -> dict[str, Any]:
        async with self._session_factory() as session:
            record = await store_file(
                session,
                org_id=self.org_id,
                workspace_id=self.workspace_id,
                filename=filename,
                content=content,
                content_type=content_type,
                source=source,
            )
            await session.commit()
            return file_ref(record)

    async def load(self, file_id: str) -> tuple[dict[str, Any], bytes]:
        async with self._session_factory() as session:
            record, data = await load_file(session, self.org_id, file_id)
            return file_ref(record), data
