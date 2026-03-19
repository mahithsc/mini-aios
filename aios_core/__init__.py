__all__ = ["create_agent", "cron_manager", "dream"]


def __getattr__(name: str):
    if name == "create_agent":
        from .agent import create_agent

        return create_agent
    if name == "cron_manager":
        from .crons import cron_manager

        return cron_manager
    if name == "dream":
        from .dream import dream

        return dream
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
