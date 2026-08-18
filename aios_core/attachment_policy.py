"""Transport-neutral attachment type policy shared by runtime and server."""

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

__all__ = [
    "AUDIO_FILE_EXTENSIONS",
    "AUDIO_MIME_TYPES",
    "DOCUMENT_MIME_TYPES",
    "TEXT_FILE_EXTENSIONS",
    "TEXT_MIME_TYPES",
]
