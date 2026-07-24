from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import HTTPException, UploadFile, status
from pydantic import BaseModel

from server.uploads import AUDIO_FILE_EXTENSIONS, AUDIO_MIME_TYPES

load_dotenv()

GROQ_API_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_TRANSCRIPTION_MODEL = os.getenv("GROQ_TRANSCRIPTION_MODEL", "whisper-large-v3")
GROQ_CLEANUP_MODEL = os.getenv("GROQ_CLEANUP_MODEL", "llama-3.1-8b-instant")
GROQ_REQUEST_TIMEOUT_SECONDS = 120.0
MAX_TRANSCRIPTION_AUDIO_BYTES = 25 * 1024 * 1024
SUPPORTED_AUDIO_EXTENSIONS = {*AUDIO_FILE_EXTENSIONS, ".webm"}

TRANSCRIPT_CLEANUP_SYSTEM_PROMPT = (
    "You clean speech-to-text transcripts. Fix punctuation, capitalization, spacing, "
    "and obvious transcription artifacts. Remove filler words only when they do not "
    "change meaning. Do not summarize. Do not add new information. Preserve the "
    "speaker's intent, wording, and technical terms. Return only the cleaned transcript text."
)


class TranscriptionResponse(BaseModel):
    transcript: str
    rawTranscript: str
    cleanupError: str | None = None
    startedAt: int | None = None
    endedAt: int | None = None
    transcriptionModel: str = GROQ_TRANSCRIPTION_MODEL
    cleanupModel: str = GROQ_CLEANUP_MODEL


def _get_groq_api_key() -> str:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GROQ_API_KEY is not configured.",
        )
    return api_key


def _normalize_mime_type(mime_type: str | None) -> str | None:
    if not mime_type:
        return None

    normalized = mime_type.split(";", 1)[0].strip().lower()
    return normalized or None


def _get_upload_mime_type(upload: UploadFile, mime_type: str | None) -> str | None:
    return _normalize_mime_type(mime_type or upload.content_type)


def _is_supported_audio(upload: UploadFile, mime_type: str | None) -> bool:
    if mime_type in AUDIO_MIME_TYPES:
        return True

    extension = Path(upload.filename or "").suffix.lower()
    return extension in SUPPORTED_AUDIO_EXTENSIONS


async def _read_audio_bytes(upload: UploadFile) -> bytes:
    audio_bytes = await upload.read()
    if not audio_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Transcription audio file is empty.",
        )

    if len(audio_bytes) > MAX_TRANSCRIPTION_AUDIO_BYTES:
        limit_mb = MAX_TRANSCRIPTION_AUDIO_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Transcription audio exceeds the {limit_mb} MB limit.",
        )

    return audio_bytes


def _groq_error_detail(prefix: str, response: httpx.Response) -> str:
    response_text = response.text.strip()
    if len(response_text) > 500:
        response_text = f"{response_text[:500]}..."

    return (
        f"{prefix} failed with {response.status_code} {response.reason_phrase}"
        f"{': ' + response_text if response_text else ''}"
    )


async def _transcribe_with_groq(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    audio_bytes: bytes,
    filename: str,
    mime_type: str,
) -> str:
    response = await client.post(
        f"{GROQ_API_BASE_URL}/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        data={
            "model": GROQ_TRANSCRIPTION_MODEL,
            "response_format": "json",
        },
        files={
            "file": (filename, audio_bytes, mime_type),
        },
    )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_groq_error_detail("Groq transcription", response),
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq transcription response was not valid JSON.",
        ) from exc

    transcript = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(transcript, str):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq transcription response did not include text.",
        )

    return transcript


def _extract_chat_completion_text(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return None

    message = first_choice.get("message")
    if not isinstance(message, dict):
        return None

    content = message.get("content")
    return content if isinstance(content, str) else None


async def _clean_transcript_with_groq(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    raw_transcript: str,
) -> str:
    if not raw_transcript.strip():
        return ""

    response = await client.post(
        f"{GROQ_API_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": GROQ_CLEANUP_MODEL,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": TRANSCRIPT_CLEANUP_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": f"Clean this transcript:\n\n{raw_transcript}",
                },
            ],
        },
    )

    if response.status_code >= 400:
        raise RuntimeError(_groq_error_detail("Groq transcript cleanup", response))

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Groq transcript cleanup response was not valid JSON.") from exc

    cleaned_transcript = _extract_chat_completion_text(payload)
    if cleaned_transcript is None:
        raise RuntimeError("Groq transcript cleanup response did not include text.")

    cleaned_transcript = cleaned_transcript.strip()
    if not cleaned_transcript:
        raise RuntimeError("Groq transcript cleanup response was empty.")

    return cleaned_transcript


async def transcribe_upload(
    upload: UploadFile,
    *,
    started_at: int | None,
    ended_at: int | None,
    mime_type: str | None,
) -> TranscriptionResponse:
    effective_mime_type = _get_upload_mime_type(upload, mime_type)
    if not _is_supported_audio(upload, effective_mime_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported transcription audio type.",
        )

    filename = Path(upload.filename or "recording.webm").name or "recording.webm"
    audio_mime_type = effective_mime_type or "audio/webm"

    try:
        audio_bytes = await _read_audio_bytes(upload)
    finally:
        await upload.close()

    api_key = _get_groq_api_key()
    timeout = httpx.Timeout(GROQ_REQUEST_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(timeout=timeout) as client:
        raw_transcript = await _transcribe_with_groq(
            client,
            api_key=api_key,
            audio_bytes=audio_bytes,
            filename=filename,
            mime_type=audio_mime_type,
        )

        cleanup_error: str | None = None
        try:
            cleaned_transcript = await _clean_transcript_with_groq(
                client,
                api_key=api_key,
                raw_transcript=raw_transcript,
            )
        except Exception as exc:
            cleanup_error = str(exc)
            cleaned_transcript = raw_transcript

    return TranscriptionResponse(
        transcript=cleaned_transcript,
        rawTranscript=raw_transcript,
        cleanupError=cleanup_error,
        startedAt=started_at,
        endedAt=ended_at,
    )
