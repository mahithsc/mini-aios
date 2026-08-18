"""Public construction and streaming interfaces for the AIOS agent."""

_FACTORY_EXPORTS = {
    "DEFAULT_MODEL_ID",
    "DEFAULT_REASONING_EFFORT",
    "create_agent",
    "create_main_agent",
    "create_subagent_worker",
}
_RUNTIME_EXPORTS = {
    "AgentRunRequest",
    "AgentRuntime",
    "run_agent_to_completion",
}
_EVENT_EXPORTS = {"AgentEvent", "AgentEventKind"}

__all__ = [
    "AgentEvent",
    "AgentEventKind",
    "AgentRunRequest",
    "AgentRuntime",
    "DEFAULT_MODEL_ID",
    "DEFAULT_REASONING_EFFORT",
    "create_agent",
    "create_main_agent",
    "create_subagent_worker",
    "run_agent_to_completion",
]


def __getattr__(name: str):
    if name in _FACTORY_EXPORTS:
        from . import factory

        return getattr(factory, name)
    if name in _RUNTIME_EXPORTS:
        from . import runtime

        return getattr(runtime, name)
    if name in _EVENT_EXPORTS:
        from . import events

        return getattr(events, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
