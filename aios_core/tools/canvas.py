from __future__ import annotations

from urllib.parse import urlparse

from aios_core.workspace import PathAccessError, resolve_agent_path
from server.types.artifact import GenerativeUIArtifact

SUPPORTED_CANVAS_KINDS = {"image", "video", "file", "html"}


def _is_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _normalize_optional_string(value: object) -> str | None:
    if not _is_non_empty_string(value):
        return None
    return value.strip()


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def show_canvas(
    kind: str,
    title: str | None = None,
    url: str | None = None,
    file_path: str | None = None,
    name: str | None = None,
    mime_type: str | None = None,
    thumbnail_url: str | None = None,
    text_preview: str | None = None,
    size_bytes: int | None = None,
):
    """
    Describe media or a file for the chat canvas.

    This is a backend contract only for now. The desktop app can later map this
    structured payload into a per-chat canvas panel.
    """
    normalized_kind = kind.strip().lower() if isinstance(kind, str) else ""
    if normalized_kind not in SUPPORTED_CANVAS_KINDS:
        supported = ", ".join(sorted(SUPPORTED_CANVAS_KINDS))
        return f"error: kind must be one of {supported}"

    normalized_url = _normalize_optional_string(url)
    normalized_file_path = _normalize_optional_string(file_path)
    normalized_title = _normalize_optional_string(title)
    normalized_name = _normalize_optional_string(name)
    normalized_mime_type = _normalize_optional_string(mime_type)
    normalized_thumbnail_url = _normalize_optional_string(thumbnail_url)
    normalized_text_preview = _normalize_optional_string(text_preview)

    if normalized_file_path is not None:
        try:
            normalized_file_path = str(resolve_agent_path(normalized_file_path))
        except PathAccessError as exc:
            return f"error: {exc}"

    if not normalized_url and not normalized_file_path:
        return "error: either url or file_path is required"
    if normalized_url and not _is_http_url(normalized_url):
        return "error: url must be an http(s) URL"
    if normalized_thumbnail_url and not _is_http_url(normalized_thumbnail_url):
        return "error: thumbnail_url must be an http(s) URL"

    if size_bytes is not None:
        if not isinstance(size_bytes, int):
            return "error: size_bytes must be an integer"
        if size_bytes < 0:
            return "error: size_bytes must be >= 0"

    if normalized_kind == "html":
        if normalized_mime_type is None:
            normalized_mime_type = "text/html"

    artifact = {
        "version": 1,
        "kind": normalized_kind,
        "title": normalized_title,
        "url": normalized_url,
        "filePath": normalized_file_path,
        "name": normalized_name,
        "mimeType": normalized_mime_type,
        "thumbnailUrl": normalized_thumbnail_url,
        "textPreview": normalized_text_preview,
        "sizeBytes": size_bytes,
    }

    if normalized_kind == "html" and normalized_file_path is not None:
        artifact["htmlArtifact"] = GenerativeUIArtifact(
            id=(normalized_name or normalized_title or "artifact").replace(" ", "-").lower(),
            title=normalized_title,
            filePath=normalized_file_path,
            url=normalized_url or "",
            mimeType="text/html",
            textPreview=normalized_text_preview,
        ).model_dump(mode="json")

    return {
        "ok": True,
        "type": "canvas_artifact",
        "artifact": {key: value for key, value in artifact.items() if value is not None},
        "message": "Canvas artifact prepared."
    }
