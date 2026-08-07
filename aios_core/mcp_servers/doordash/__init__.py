"""First-party DoorDash MCP adapter backed by dd-cli."""

from .client import DoorDashCLIClient, DoorDashCLIError

__all__ = ["DoorDashCLIClient", "DoorDashCLIError"]
