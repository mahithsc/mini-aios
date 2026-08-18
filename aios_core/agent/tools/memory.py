from __future__ import annotations

from typing import Literal

from ...memory import mutate_memory


def memory(
    action: Literal["add", "replace", "remove"],
    target: Literal["memory", "user"] = "memory",
    content: str | None = None,
    old_text: str | None = None,
):
    """Manage bounded persistent memory shared across conversations.

    Add durable facts and preferences proactively. Use ``target='user'`` for
    identity, preferences, communication style, and workflow habits. Use
    ``target='memory'`` for environment facts, project conventions, important
    decisions, completed milestones, and reusable lessons. Do not save secrets,
    raw transcripts, temporary task state, or facts that are easy to rediscover.

    ``replace`` and ``remove`` identify exactly one entry using a short unique
    substring in ``old_text``. There is intentionally no read action because the
    current snapshot is already present in the system prompt.
    """
    return mutate_memory(
        action=action,
        target=target,
        content=content,
        old_text=old_text,
    )
