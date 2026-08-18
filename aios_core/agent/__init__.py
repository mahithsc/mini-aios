"""Agent construction and runtime integration."""

__all__ = [
    "DEFAULT_MODEL_ID",
    "DEFAULT_REASONING_EFFORT",
    "create_agent",
    "create_main_agent",
    "create_subagent_worker",
]


def __getattr__(name: str):
    if name in __all__:
        from . import factory

        return getattr(factory, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
