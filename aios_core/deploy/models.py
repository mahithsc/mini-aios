"""Data model for the deploy Supervisor.

A Project is a durable unit of work (source + spec + status). A Spec says how to
run it. Kept dependency-free so it's trivial to construct in tests and persist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Spec:
    run: list[str]                 # e.g. ["python", "app.py"] — the in-container run command
    port: int                      # the port the app listens on inside the container
    image: str = "python:3.12-slim"
    env: dict[str, str] = field(default_factory=dict)
    prepare: list[list[str]] = field(default_factory=list)  # dep-install commands
    # Runtime resource limits (applied as `docker run` flags).
    memory_mb: int = 512
    cpus: float = 1.0
    pids_limit: int = 256


@dataclass
class Project:
    slug: str
    source_dir: Path
    spec: Spec
    id: str = ""
    status: str = "draft"          # draft | building | running | stopped | error
