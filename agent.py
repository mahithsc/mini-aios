"""Backward-compatible import for the canonical AIOS agent factory."""

from aios_core.agent import DEFAULT_MODEL_ID, DEFAULT_REASONING_EFFORT, create_agent

__all__ = ["DEFAULT_MODEL_ID", "DEFAULT_REASONING_EFFORT", "create_agent"]
