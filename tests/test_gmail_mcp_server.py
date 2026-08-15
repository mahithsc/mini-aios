from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterable
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from aios_core.integrations.google import GMAIL_READ_SCOPES, GMAIL_SEND_SCOPES
from aios_core.integrations.google_mcp import (
    LocalGmailMCPTools,
    gmail_server_parameters,
)
from aios_core.mcp_servers.gmail.client import (
    GmailAPIClient,
    GmailAPIError,
)


class StubTokenProvider:
    def __init__(self) -> None:
        self.requests: list[tuple[str, ...]] = []
        self.rejected_tokens: list[str | None] = []

    async def access_token(
        self,
        *,
        required_scopes: Iterable[str],
        rejected_token: str | None = None,
    ) -> str:
        self.requests.append(tuple(required_scopes))
        self.rejected_tokens.append(rejected_token)
        return (
            "refreshed-access-token"
            if rejected_token is not None
            else "short-lived-access-token"
        )


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decoded_message(value: str):
    padded = value + ("=" * (-len(value) % 4))
    return BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(padded)
    )


def test_search_returns_bounded_useful_summaries() -> None:
    token_provider = StubTokenProvider()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == ("Bearer short-lived-access-token")
        if request.url.path.endswith("/messages"):
            assert request.url.params["q"] == "newer_than:7d"
            assert request.url.params["maxResults"] == "25"
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {"id": "message-1", "threadId": "thread-1"},
                    ],
                    "nextPageToken": "next-page",
                    "resultSizeEstimate": 42,
                },
            )
        assert request.url.path.endswith("/messages/message-1")
        assert request.url.params["format"] == "metadata"
        assert request.url.params.get_list("metadataHeaders") == [
            "From",
            "To",
            "Subject",
            "Date",
        ]
        return httpx.Response(
            200,
            json={
                "id": "message-1",
                "threadId": "thread-1",
                "internalDate": "1710000000000",
                "snippet": "Quarterly update",
                "labelIds": ["INBOX"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "sender@example.com"},
                        {"name": "To", "value": "user@example.com"},
                        {"name": "Subject", "value": "Q1 update"},
                        {"name": "Date", "value": "Sat, 9 Mar 2024 16:00:00 +0000"},
                    ]
                },
            },
        )

    client = GmailAPIClient(
        token_provider,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    result = asyncio.run(
        client.search_messages(query=" newer_than:7d ", max_results=500)
    )

    assert token_provider.requests == [GMAIL_READ_SCOPES]
    assert len(requests) == 2
    assert result["resultSizeEstimate"] == 42
    assert result["nextPageToken"] == "next-page"
    assert result["messages"] == [
        {
            "id": "message-1",
            "threadId": "thread-1",
            "from": "sender@example.com",
            "to": "user@example.com",
            "subject": "Q1 update",
            "date": "Sat, 9 Mar 2024 16:00:00 +0000",
            "receivedAt": "2024-03-09T16:00:00+00:00",
            "snippet": "Quarterly update",
            "labelIds": ["INBOX"],
        }
    ]


def test_message_normalizes_text_and_never_downloads_attachments() -> None:
    token_provider = StubTokenProvider()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "message-1",
                "threadId": "thread-1",
                "historyId": "9",
                "internalDate": "1710000000000",
                "snippet": "hello",
                "labelIds": ["INBOX"],
                "sizeEstimate": 1234,
                "payload": {
                    "mimeType": "multipart/mixed",
                    "headers": [
                        {"name": "From", "value": "sender@example.com"},
                        {"name": "Subject", "value": "Hello"},
                    ],
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": _encoded("Hello from Gmail")},
                        },
                        {
                            "mimeType": "application/pdf",
                            "filename": "report.pdf",
                            "body": {
                                "attachmentId": "attachment-1",
                                "size": 1000,
                            },
                        },
                    ],
                },
            },
        )

    client = GmailAPIClient(
        token_provider,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    result = asyncio.run(client.get_message("message-1"))

    assert len(requests) == 1
    assert "/attachments/" not in str(requests[0].url)
    assert result["body"] == "Hello from Gmail"
    assert result["bodyTruncated"] is False
    assert result["attachments"] == [
        {
            "filename": "report.pdf",
            "mimeType": "application/pdf",
            "size": 1000,
            "attachmentId": "attachment-1",
        }
    ]


def test_gmail_errors_are_stable_and_do_not_echo_access_tokens() -> None:
    token_provider = StubTokenProvider()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "message": "Token short-lived-access-token is invalid",
                }
            },
        )

    client = GmailAPIClient(
        token_provider,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    with pytest.raises(GmailAPIError) as error:
        asyncio.run(client.list_labels())

    assert error.value.code == "gmail_unauthorized"
    assert error.value.status_code == 401
    assert "short-lived-access-token" not in str(error.value)


def test_unauthorized_access_token_is_refreshed_once() -> None:
    token_provider = StubTokenProvider()
    authorizations: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers["authorization"]
        authorizations.append(authorization)
        if authorization == "Bearer short-lived-access-token":
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(
            200,
            json={"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"}]},
        )

    client = GmailAPIClient(
        token_provider,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    result = asyncio.run(client.list_labels())

    assert authorizations == [
        "Bearer short-lived-access-token",
        "Bearer refreshed-access-token",
    ]
    assert token_provider.rejected_tokens == [
        None,
        "short-lived-access-token",
    ]
    assert result["labels"][0]["id"] == "INBOX"


def test_send_email_builds_mime_message_and_uses_send_scope() -> None:
    token_provider = StubTokenProvider()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path.endswith("/users/me/messages/send")
        assert request.headers["authorization"] == "Bearer short-lived-access-token"
        payload = json.loads(request.content)
        message = _decoded_message(payload["raw"])
        assert message["To"] == "Taylor Example <taylor@example.com>"
        assert message["Cc"] == "copy@example.com"
        assert message["Bcc"] == "private@example.com"
        assert message["Reply-To"] == "replies@example.com"
        assert message["Subject"] == "Project update"
        assert message.get_content_type() == "text/plain"
        assert message.get_content().rstrip() == "The project is on schedule."
        return httpx.Response(
            200,
            json={
                "id": "sent-message",
                "threadId": "sent-thread",
                "labelIds": ["SENT"],
            },
        )

    client = GmailAPIClient(
        token_provider,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    result = asyncio.run(
        client.send_email(
            to=["Taylor Example <taylor@example.com>"],
            cc=["copy@example.com"],
            bcc=["private@example.com"],
            reply_to="replies@example.com",
            subject="Project update",
            body="The project is on schedule.",
        )
    )

    assert token_provider.requests == [GMAIL_SEND_SCOPES]
    assert len(requests) == 1
    assert result == {
        "sent": True,
        "id": "sent-message",
        "threadId": "sent-thread",
        "labelIds": ["SENT"],
    }


def test_send_email_rejects_header_injection_before_request() -> None:
    token_provider = StubTokenProvider()
    client = GmailAPIClient(token_provider)

    with pytest.raises(GmailAPIError) as error:
        asyncio.run(
            client.send_email(
                to=["recipient@example.com\nBcc: attacker@example.com"],
                subject="Hello",
                body="Safe body",
            )
        )

    assert error.value.code == "invalid_to"
    assert token_provider.requests == []


def test_stdio_server_advertises_read_and_send_tools() -> None:
    async def list_tools():
        async with stdio_client(gmail_server_parameters()) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                return (await session.list_tools()).tools

    tools = asyncio.run(list_tools())

    assert [tool.name for tool in tools] == [
        "search_messages",
        "get_message",
        "get_thread",
        "list_labels",
        "send_email",
    ]
    assert all(tool.annotations.readOnlyHint for tool in tools[:-1])
    assert tools[-1].annotations.readOnlyHint is False
    assert tools[-1].annotations.idempotentHint is False
    assert all(tool.annotations.destructiveHint is False for tool in tools)


def test_agno_adapter_registers_prefixed_local_tools() -> None:
    async def connect_toolkit():
        toolkit = LocalGmailMCPTools()
        try:
            await toolkit.connect()
            return (
                toolkit._initialized,
                sorted(toolkit.functions),
                toolkit.functions["gmail_send_email"].requires_confirmation,
            )
        finally:
            await toolkit.close()

    initialized, tools, send_requires_confirmation = asyncio.run(connect_toolkit())

    assert initialized is True
    assert send_requires_confirmation is not True
    assert tools == [
        "gmail_get_message",
        "gmail_get_thread",
        "gmail_list_labels",
        "gmail_search_messages",
        "gmail_send_email",
    ]
