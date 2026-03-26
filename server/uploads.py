from __future__ import annotations

import mimetypes
import re
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from aios_core.workspace import resolve_workspace_path
from server.types.chat import MessageAttachment

UPLOAD_ROOT = "uploads"
MAX_ATTACHMENTS_PER_REQUEST = 10
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
TEXT_FILE_EXTENSIONS = {
    ".c",
    ".cpp",
    ".css",
    ".csv",
    ".go",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
TEXT_MIME_TYPES = {
    "application/json",
    "application/x-javascript",
    "application/x-python",
    "text/css",
    "text/csv",
    "text/html",
    "text/javascript",
    "text/markdown",
    "text/md",
    "text/plain",
    "text/x-python",
    "text/xml",
}
DOCUMENT_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    *TEXT_MIME_TYPES,
}


def _sanitize_path_segment(value: str, fallback: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("._-")
    return sanitized or fallback


def _sanitize_filename(filename: str | None) -> str:
    raw_name = Path(filename or "attachment").name
    if not raw_name:
        raw_name = "attachment"

    stem = _sanitize_path_segment(Path(raw_name).stem, "attachment")
    suffix = re.sub(r"[^A-Za-z0-9.]+", "", Path(raw_name).suffix)[:16]
    return f"{stem}{suffix}" if suffix else stem


def _get_relative_upload_dir(chat_id: str) -> Path:
    return Path(UPLOAD_ROOT) / _sanitize_path_segment(chat_id, "chat")


def _get_unique_relative_path(chat_id: str, filename: str) -> Path:
    relative_dir = _get_relative_upload_dir(chat_id)
    candidate = relative_dir / filename
    target_path = resolve_workspace_path(candidate)

    if not target_path.exists():
        return candidate

    unique_prefix = uuid.uuid4().hex[:8]
    return relative_dir / f"{unique_prefix}-{filename}"


def _infer_mime_type(upload: UploadFile, filename: str) -> str | None:
    return upload.content_type or mimetypes.guess_type(filename)[0]


def _is_supported_attachment(mime_type: str | None, filename: str) -> bool:
    if mime_type and mime_type.startswith("image/"):
        return True
    if mime_type in DOCUMENT_MIME_TYPES:
        return True
    return Path(filename).suffix.lower() in TEXT_FILE_EXTENSIONS


def _attachment_kind(mime_type: str | None) -> str:
    if mime_type and mime_type.startswith("image/"):
        return "image"
    return "file"


async def save_uploads(chat_id: str, files: list[UploadFile]) -> list[MessageAttachment]:
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select at least one file to attach.",
        )

    if len(files) > MAX_ATTACHMENTS_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"You can upload up to {MAX_ATTACHMENTS_PER_REQUEST} files at a time.",
        )

    saved_attachments: list[MessageAttachment] = []

    for upload in files:
        safe_filename = _sanitize_filename(upload.filename)
        mime_type = _infer_mime_type(upload, safe_filename)

        if not _is_supported_attachment(mime_type, safe_filename):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported attachment type for {safe_filename}.",
            )

        relative_path = _get_unique_relative_path(chat_id, safe_filename)
        absolute_path = resolve_workspace_path(relative_path)
        absolute_path.parent.mkdir(parents=True, exist_ok=True)

        size_bytes = 0
        try:
            with absolute_path.open("wb") as target:
                while True:
                    chunk = await upload.read(CHUNK_SIZE)
                    if not chunk:
                        break

                    size_bytes += len(chunk)
                    if size_bytes > MAX_ATTACHMENT_BYTES:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"{safe_filename} exceeds the 20 MB attachment limit.",
                        )

                    target.write(chunk)
        except HTTPException:
            absolute_path.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()

        saved_attachments.append(
            MessageAttachment(
                id=str(uuid.uuid4()),
                kind=_attachment_kind(mime_type),
                name=safe_filename,
                filePath=str(relative_path),
                mimeType=mime_type,
                sizeBytes=size_bytes,
                uploadedAt=int(datetime.now().timestamp() * 1000),
            )
        )

    return saved_attachments
