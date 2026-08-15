from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr, getaddresses
from html.parser import HTMLParser
from typing import Any

import httpx

from ...integrations.google import GMAIL_READ_SCOPES, GMAIL_SEND_SCOPES
from ..credentials import AccessTokenProvider

GMAIL_API_BASE_URL = "https://gmail.googleapis.com/gmail/v1"
MAX_SEARCH_RESULTS = 25
MAX_QUERY_LENGTH = 2_048
MAX_MESSAGE_BODY_CHARS = 50_000
MAX_THREAD_MESSAGES = 20
MAX_THREAD_BODY_CHARS = 10_000
MAX_RECIPIENTS = 50
MAX_SUBJECT_CHARS = 500
MAX_OUTGOING_BODY_CHARS = 200_000

_EMAIL_ADDRESS_PATTERN = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")

HttpClientFactory = Callable[[], httpx.AsyncClient]


class GmailAPIError(RuntimeError):
    """Stable error returned when Gmail cannot fulfill a tool request."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "gmail_error",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return "\n".join(self.parts)


class GmailAPIClient:
    """Small, bounded adapter over Gmail's REST API."""

    def __init__(
        self,
        token_provider: AccessTokenProvider,
        *,
        http_client_factory: HttpClientFactory | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=30.0)
        )

    async def search_messages(
        self,
        *,
        query: str = "",
        max_results: int = 10,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        query = query.strip()
        if len(query) > MAX_QUERY_LENGTH:
            raise GmailAPIError(
                f"Gmail search queries cannot exceed {MAX_QUERY_LENGTH} characters",
                code="invalid_query",
            )
        limit = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
        token = await self._access_token()
        params: dict[str, str | int] = {"maxResults": limit}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token

        async with self._http_client_factory() as client:
            result = await self._get_json(
                client,
                token,
                "/users/me/messages",
                params=params,
            )
            raw_messages = result.get("messages")
            message_refs = raw_messages if isinstance(raw_messages, list) else []
            detail_results = await asyncio.gather(
                *(
                    self._get_json(
                        client,
                        token,
                        f"/users/me/messages/{message_id}",
                        params=[
                            ("format", "metadata"),
                            ("metadataHeaders", "From"),
                            ("metadataHeaders", "To"),
                            ("metadataHeaders", "Subject"),
                            ("metadataHeaders", "Date"),
                        ],
                    )
                    for item in message_refs
                    if isinstance(item, dict)
                    and isinstance((message_id := item.get("id")), str)
                ),
            )

        response: dict[str, Any] = {
            "query": query,
            "messages": [self._message_summary(message) for message in detail_results],
            "resultSizeEstimate": _integer(result.get("resultSizeEstimate")),
        }
        next_page_token = result.get("nextPageToken")
        if isinstance(next_page_token, str) and next_page_token:
            response["nextPageToken"] = next_page_token
        return response

    async def get_message(self, message_id: str) -> dict[str, Any]:
        message_id = _require_resource_id(message_id, "message")
        token = await self._access_token()
        async with self._http_client_factory() as client:
            message = await self._get_json(
                client,
                token,
                f"/users/me/messages/{message_id}",
                params={"format": "full"},
            )
        return self._normalize_message(
            message,
            max_body_chars=MAX_MESSAGE_BODY_CHARS,
        )

    async def get_thread(self, thread_id: str) -> dict[str, Any]:
        thread_id = _require_resource_id(thread_id, "thread")
        token = await self._access_token()
        async with self._http_client_factory() as client:
            thread = await self._get_json(
                client,
                token,
                f"/users/me/threads/{thread_id}",
                params={"format": "full"},
            )

        raw_messages = thread.get("messages")
        messages = raw_messages if isinstance(raw_messages, list) else []
        selected = messages[-MAX_THREAD_MESSAGES:]
        return {
            "id": _string(thread.get("id")),
            "historyId": _string(thread.get("historyId")),
            "messageCount": len(messages),
            "messagesTruncated": len(messages) > len(selected),
            "messages": [
                self._normalize_message(
                    message,
                    max_body_chars=MAX_THREAD_BODY_CHARS,
                )
                for message in selected
                if isinstance(message, dict)
            ],
        }

    async def list_labels(self) -> dict[str, Any]:
        token = await self._access_token()
        async with self._http_client_factory() as client:
            result = await self._get_json(
                client,
                token,
                "/users/me/labels",
            )
        raw_labels = result.get("labels")
        labels = raw_labels if isinstance(raw_labels, list) else []
        return {
            "labels": [
                {
                    "id": _string(label.get("id")),
                    "name": _string(label.get("name")),
                    "type": _string(label.get("type")),
                    "messageListVisibility": _optional_string(
                        label.get("messageListVisibility")
                    ),
                    "labelListVisibility": _optional_string(
                        label.get("labelListVisibility")
                    ),
                }
                for label in labels
                if isinstance(label, dict)
            ]
        }

    async def send_email(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
        body_format: str = "plain",
    ) -> dict[str, Any]:
        """Send one bounded MIME message without attachments."""

        to_addresses = _mailboxes(to, field="to", required=True)
        cc_addresses = _mailboxes(cc or [], field="cc")
        bcc_addresses = _mailboxes(bcc or [], field="bcc")
        recipient_count = (
            len(to_addresses) + len(cc_addresses) + len(bcc_addresses)
        )
        if recipient_count > MAX_RECIPIENTS:
            raise GmailAPIError(
                f"An email cannot have more than {MAX_RECIPIENTS} recipients",
                code="too_many_recipients",
            )

        if "\r" in subject or "\n" in subject:
            raise GmailAPIError(
                "Email subjects cannot contain line breaks",
                code="invalid_subject",
            )
        if len(subject) > MAX_SUBJECT_CHARS:
            raise GmailAPIError(
                f"Email subjects cannot exceed {MAX_SUBJECT_CHARS} characters",
                code="subject_too_long",
            )
        if not body:
            raise GmailAPIError(
                "Email body is required",
                code="missing_body",
            )
        if len(body) > MAX_OUTGOING_BODY_CHARS:
            raise GmailAPIError(
                (
                    "Email body cannot exceed "
                    f"{MAX_OUTGOING_BODY_CHARS} characters"
                ),
                code="body_too_long",
            )
        normalized_format = body_format.strip().lower()
        if normalized_format not in {"plain", "html"}:
            raise GmailAPIError(
                "Email body format must be plain or html",
                code="invalid_body_format",
            )

        message = EmailMessage()
        message["To"] = ", ".join(to_addresses)
        if cc_addresses:
            message["Cc"] = ", ".join(cc_addresses)
        if bcc_addresses:
            message["Bcc"] = ", ".join(bcc_addresses)
        if reply_to:
            message["Reply-To"] = _mailboxes(
                [reply_to],
                field="reply_to",
                required=True,
            )[0]
        message["Subject"] = subject
        message.set_content(
            body,
            subtype="html" if normalized_format == "html" else "plain",
            charset="utf-8",
        )
        raw_message = base64.urlsafe_b64encode(
            message.as_bytes(policy=SMTP)
        ).decode("ascii").rstrip("=")

        token = await self._access_token(required_scopes=GMAIL_SEND_SCOPES)
        async with self._http_client_factory() as client:
            result = await self._post_json(
                client,
                token,
                "/users/me/messages/send",
                json_body={"raw": raw_message},
                required_scopes=GMAIL_SEND_SCOPES,
            )
        return {
            "sent": True,
            "id": _string(result.get("id")),
            "threadId": _string(result.get("threadId")),
            "labelIds": _string_list(result.get("labelIds")),
        }

    async def _access_token(
        self,
        *,
        required_scopes: tuple[str, ...] = GMAIL_READ_SCOPES,
        rejected_token: str | None = None,
    ) -> str:
        return await self._token_provider.access_token(
            required_scopes=required_scopes,
            rejected_token=rejected_token,
        )

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        token: str,
        path: str,
        *,
        params: Mapping[str, str | int] | list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        response = await self._request(client, token, path, params=params)
        if response.status_code == 401:
            refreshed_token = await self._access_token(rejected_token=token)
            response = await self._request(
                client,
                refreshed_token,
                path,
                params=params,
            )

        if response.is_error:
            raise _response_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GmailAPIError(
                "Gmail returned an invalid response",
                code="invalid_gmail_response",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise GmailAPIError(
                "Gmail returned an invalid response",
                code="invalid_gmail_response",
                status_code=response.status_code,
            )
        return payload

    async def _post_json(
        self,
        client: httpx.AsyncClient,
        token: str,
        path: str,
        *,
        json_body: Mapping[str, Any],
        required_scopes: tuple[str, ...],
    ) -> dict[str, Any]:
        response = await self._request(
            client,
            token,
            path,
            method="POST",
            json_body=json_body,
        )
        if response.status_code == 401:
            refreshed_token = await self._access_token(
                required_scopes=required_scopes,
                rejected_token=token,
            )
            response = await self._request(
                client,
                refreshed_token,
                path,
                method="POST",
                json_body=json_body,
            )
        if response.is_error:
            raise _response_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GmailAPIError(
                "Gmail returned an invalid response",
                code="invalid_gmail_response",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise GmailAPIError(
                "Gmail returned an invalid response",
                code="invalid_gmail_response",
                status_code=response.status_code,
            )
        return payload

    async def _request(
        self,
        client: httpx.AsyncClient,
        token: str,
        path: str,
        *,
        method: str = "GET",
        params: Mapping[str, str | int] | list[tuple[str, str]] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            return await client.request(
                method,
                f"{GMAIL_API_BASE_URL}{path}",
                params=params,
                json=json_body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Cache-Control": "no-store",
                },
            )
        except httpx.HTTPError as exc:
            raise GmailAPIError(
                "Gmail is temporarily unreachable",
                code="gmail_unavailable",
            ) from exc

    def _message_summary(self, message: dict[str, Any]) -> dict[str, Any]:
        headers = _headers(message.get("payload"))
        return {
            "id": _string(message.get("id")),
            "threadId": _string(message.get("threadId")),
            "from": headers.get("from"),
            "to": headers.get("to"),
            "subject": headers.get("subject"),
            "date": headers.get("date"),
            "receivedAt": _received_at(message.get("internalDate")),
            "snippet": _string(message.get("snippet")),
            "labelIds": _string_list(message.get("labelIds")),
        }

    def _normalize_message(
        self,
        message: dict[str, Any],
        *,
        max_body_chars: int,
    ) -> dict[str, Any]:
        payload = message.get("payload")
        payload_dict = payload if isinstance(payload, dict) else {}
        headers = _headers(payload_dict)
        plain_parts: list[str] = []
        html_parts: list[str] = []
        attachments: list[dict[str, Any]] = []
        _collect_parts(
            payload_dict,
            plain_parts=plain_parts,
            html_parts=html_parts,
            attachments=attachments,
        )
        body = "\n\n".join(part for part in plain_parts if part).strip()
        if not body and html_parts:
            extractor = _HTMLTextExtractor()
            extractor.feed("\n".join(html_parts))
            body = extractor.text().strip()
        body_truncated = len(body) > max_body_chars
        if body_truncated:
            body = body[:max_body_chars]

        return {
            "id": _string(message.get("id")),
            "threadId": _string(message.get("threadId")),
            "historyId": _string(message.get("historyId")),
            "from": headers.get("from"),
            "to": headers.get("to"),
            "cc": headers.get("cc"),
            "bcc": headers.get("bcc"),
            "subject": headers.get("subject"),
            "date": headers.get("date"),
            "messageId": headers.get("message-id"),
            "receivedAt": _received_at(message.get("internalDate")),
            "snippet": _string(message.get("snippet")),
            "labelIds": _string_list(message.get("labelIds")),
            "body": body,
            "bodyTruncated": body_truncated,
            "attachments": attachments,
            "sizeEstimate": _integer(message.get("sizeEstimate")),
        }


def _collect_parts(
    part: dict[str, Any],
    *,
    plain_parts: list[str],
    html_parts: list[str],
    attachments: list[dict[str, Any]],
) -> None:
    mime_type = _string(part.get("mimeType")).lower()
    filename = _string(part.get("filename"))
    body_value = part.get("body")
    body = body_value if isinstance(body_value, dict) else {}
    attachment_id = body.get("attachmentId")
    if filename or isinstance(attachment_id, str):
        attachments.append(
            {
                "filename": filename,
                "mimeType": mime_type,
                "size": _integer(body.get("size")),
                "attachmentId": (
                    attachment_id if isinstance(attachment_id, str) else None
                ),
            }
        )
    elif isinstance(body.get("data"), str):
        decoded = _decode_body(body["data"])
        if mime_type == "text/plain":
            plain_parts.append(decoded)
        elif mime_type == "text/html":
            html_parts.append(decoded)

    raw_parts = part.get("parts")
    if isinstance(raw_parts, list):
        for child in raw_parts:
            if isinstance(child, dict):
                _collect_parts(
                    child,
                    plain_parts=plain_parts,
                    html_parts=html_parts,
                    attachments=attachments,
                )


def _decode_body(value: str) -> str:
    try:
        padded = value + ("=" * (-len(value) % 4))
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _headers(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    raw_headers = payload.get("headers")
    if not isinstance(raw_headers, list):
        return {}
    result: dict[str, str] = {}
    for header in raw_headers:
        if not isinstance(header, dict):
            continue
        name = header.get("name")
        value = header.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result[name.lower()] = value
    return result


def _response_error(response: httpx.Response) -> GmailAPIError:
    message = "Gmail rejected the request"
    code = "gmail_request_failed"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            provider_message = error.get("message")
            provider_status = error.get("status")
            if isinstance(provider_message, str) and provider_message:
                message = provider_message
            if isinstance(provider_status, str) and provider_status:
                code = provider_status.lower()
    if response.status_code == 401:
        message = "The Gmail access token is no longer valid"
        code = "gmail_unauthorized"
    elif response.status_code == 403:
        code = "gmail_forbidden"
    elif response.status_code == 404:
        code = "gmail_not_found"
    elif response.status_code == 429:
        code = "gmail_rate_limited"
    return GmailAPIError(
        message,
        code=code,
        status_code=response.status_code,
    )


def _received_at(value: Any) -> str | None:
    try:
        timestamp = int(value) / 1_000
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _require_resource_id(value: str, resource: str) -> str:
    value = value.strip()
    if not value or len(value) > 256 or any(char.isspace() for char in value):
        raise GmailAPIError(
            f"A valid Gmail {resource} ID is required",
            code=f"invalid_{resource}_id",
        )
    return value


def _mailboxes(
    values: list[str],
    *,
    field: str,
    required: bool = False,
) -> list[str]:
    if required and not values:
        raise GmailAPIError(
            f"Email {field} recipient is required",
            code=f"missing_{field}",
        )
    normalized: list[str] = []
    for raw_value in values:
        value = raw_value.strip()
        if (
            not value
            or len(value) > 512
            or "\r" in value
            or "\n" in value
        ):
            raise GmailAPIError(
                f"Email {field} contains an invalid address",
                code=f"invalid_{field}",
            )
        parsed = getaddresses([value])
        if len(parsed) != 1:
            raise GmailAPIError(
                f"Email {field} contains an invalid address",
                code=f"invalid_{field}",
            )
        display_name, address = parsed[0]
        if not _EMAIL_ADDRESS_PATTERN.fullmatch(address):
            raise GmailAPIError(
                f"Email {field} contains an invalid address",
                code=f"invalid_{field}",
            )
        normalized.append(formataddr((display_name, address)))
    return normalized


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
