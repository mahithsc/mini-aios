from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class GenerativeUIArtifact(BaseModel):
    id: str | None = None
    chatId: str | None = None
    kind: Literal["html"] = "html"
    title: str | None = None
    filePath: str
    url: str
    mimeType: Literal["text/html"] = "text/html"
    textPreview: str | None = None
