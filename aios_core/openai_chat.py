from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Dict, List, Optional, Type, Union

import httpx
from agno.models.message import Message
from agno.models.openai import OpenAIChat
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from openai import APITimeoutError
from pydantic import BaseModel


MODEL_STREAM_IDLE_COMPLETION_SECONDS = 3.0


class AiosOpenAIChat(OpenAIChat):
    """OpenAI chat model that stops at the provider's finish signal.

    Some OpenAI-compatible gateways send the final chunk with ``finish_reason``
    but leave the HTTP/SSE response open. Agno normally waits for the transport
    to close before it emits ``RunContentCompleted``. Stopping at the provider's
    semantic finish signal lets the rest of Agno's run lifecycle complete.
    """

    async def ainvoke_stream(
        self,
        messages: List[Message],
        assistant_message: Message,
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[Union[str, Dict]] = None,
        run_response: Optional[Union[RunOutput, TeamRunOutput]] = None,
        compress_tool_results: bool = False,
    ) -> AsyncIterator[ModelResponse]:
        assistant_message.metrics.start_timer()
        received_substantive_output = False

        async_stream = await self.get_async_client().chat.completions.create(
            model=self.id,
            messages=[
                self._format_message(message, compress_tool_results)
                for message in messages
            ],
            stream=True,
            stream_options={"include_usage": True},
            timeout=httpx.Timeout(
                30.0,
                read=MODEL_STREAM_IDLE_COMPLETION_SECONDS,
            ),
            **self.get_request_params(
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                run_response=run_response,
            ),
        )

        try:
            async with async_stream:
                completion_deadline: float | None = None
                loop = asyncio.get_running_loop()

                async for chunk in async_stream:
                    model_response = self._parse_provider_response_delta(chunk)
                    has_substantive_output = any(
                        (
                            model_response.content not in (None, ""),
                            bool(model_response.tool_calls),
                            model_response.reasoning_content not in (None, ""),
                            model_response.audio is not None,
                        )
                    )
                    if has_substantive_output:
                        received_substantive_output = True
                        completion_deadline = (
                            loop.time()
                            + MODEL_STREAM_IDLE_COMPLETION_SECONDS
                        )
                    elif (
                        completion_deadline is not None
                        and loop.time() >= completion_deadline
                    ):
                        return

                    yield model_response

                    if (
                        chunk.choices
                        and chunk.choices[0].finish_reason is not None
                    ):
                        return
        except (APITimeoutError, httpx.ReadTimeout):
            if received_substantive_output:
                return
            raise
        finally:
            assistant_message.metrics.stop_timer()
