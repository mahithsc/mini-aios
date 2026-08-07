from __future__ import annotations

import mimetypes
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from fastapi import HTTPException, UploadFile, status

from aios_core.workspace import resolve_workspace_path
from server.types.chat import MessageAttachment

UPLOAD_ROOT = "uploads"
MAX_ATTACHMENTS_PER_REQUEST = 10
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_AUDIO_ATTACHMENT_BYTES = 100 * 1024 * 1024
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
AUDIO_FILE_EXTENSIONS = {
    ".m4a",
    ".mp3",
    ".ogg",
    ".wav",
}
AUDIO_MIME_TYPES = {
    "audio/m4a",
    "audio/mp3",
    "audio/mp4",
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
    "audio/x-m4a",
    "audio/x-wav",
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


def _candidate_relative_paths(filename: str):
    relative_dir = Path(UPLOAD_ROOT)
    source_name = Path(filename)
    yield relative_dir / filename
    number = 2
    while True:
        candidate_name = (
            f"{source_name.stem} {number}{source_name.suffix}"
            if source_name.suffix
            else f"{source_name.name} {number}"
        )
        yield relative_dir / candidate_name
        number += 1


def _reserve_unique_upload(filename: str) -> tuple[Path, Path, BinaryIO]:
    for relative_path in _candidate_relative_paths(filename):
        absolute_path = resolve_workspace_path(relative_path)
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return relative_path, absolute_path, absolute_path.open("xb")
        except FileExistsError:
            continue
    raise RuntimeError("could not reserve an upload path")


def _infer_mime_type(upload: UploadFile, filename: str) -> str | None:
    return upload.content_type or mimetypes.guess_type(filename)[0]


def _is_supported_attachment(mime_type: str | None, filename: str) -> bool:
    if mime_type and mime_type.startswith("image/"):
        return True
    if mime_type in AUDIO_MIME_TYPES:
        return True
    if mime_type in DOCUMENT_MIME_TYPES:
        return True
    extension = Path(filename).suffix.lower()
    return extension in TEXT_FILE_EXTENSIONS or extension in AUDIO_FILE_EXTENSIONS


def _attachment_kind(mime_type: str | None, filename: str) -> str:
    if mime_type and mime_type.startswith("image/"):
        return "image"
    if mime_type in AUDIO_MIME_TYPES or Path(filename).suffix.lower() in AUDIO_FILE_EXTENSIONS:
        return "audio"
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
        attachment_kind = _attachment_kind(mime_type, safe_filename)
        max_attachment_bytes = (
            MAX_AUDIO_ATTACHMENT_BYTES if attachment_kind == "audio" else MAX_ATTACHMENT_BYTES
        )

        if not _is_supported_attachment(mime_type, safe_filename):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported attachment type for {safe_filename}.",
            )

        relative_path, absolute_path, target = _reserve_unique_upload(safe_filename)

        size_bytes = 0
        try:
            with target:
                while True:
                    chunk = await upload.read(CHUNK_SIZE)
                    if not chunk:
                        break

                    size_bytes += len(chunk)
                    if size_bytes > max_attachment_bytes:
                        limit_mb = max_attachment_bytes // (1024 * 1024)
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"{safe_filename} exceeds the {limit_mb} MB attachment limit.",
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
                kind=attachment_kind,
                name=safe_filename,
                filePath=str(relative_path),
                mimeType=mime_type,
                sizeBytes=size_bytes,
                uploadedAt=int(datetime.now().timestamp() * 1000),
            )
        )

    return saved_attachments
