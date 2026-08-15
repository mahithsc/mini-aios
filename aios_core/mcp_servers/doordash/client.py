from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

MAX_ARGUMENTS_LENGTH = 100_000
MAX_OUTPUT_BYTES = 2_000_000
DEFAULT_TIMEOUT_SECONDS = 90.0
LOGIN_TIMEOUT_SECONDS = 600.0
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*bearer\s+)([^\s,;]+)"
)
_SECRET_PATTERN = re.compile(
    r"(?i)((?:access[_ -]?token|refresh[_ -]?token|cookie)"
    r"\s*[:=]\s*)([^\s,;]+)"
)


class DoorDashCLIError(RuntimeError):
    """Stable, secret-safe error raised by the DoorDash CLI adapter."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int = 502,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[
    [Sequence[str], float],
    Awaitable[CommandResult],
]


def resolve_dd_cli_executable(configured_path: str | None = None) -> str | None:
    if configured_path:
        expanded = os.path.expanduser(configured_path)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return os.path.abspath(expanded)
        return None
    return shutil.which("dd-cli")


async def _default_runner(
    command: Sequence[str],
    timeout_seconds: float,
) -> CommandResult:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise DoorDashCLIError(
            "The DoorDash CLI is not installed or is not executable",
            code="doordash_cli_unavailable",
            status_code=503,
        ) from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise DoorDashCLIError(
            "The DoorDash CLI command timed out",
            code="doordash_timeout",
            status_code=504,
        ) from exc

    return CommandResult(
        returncode=process.returncode or 0,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
    )


def _safe_error_text(value: str) -> str:
    redacted = _AUTHORIZATION_PATTERN.sub(r"\1[REDACTED]", value)
    redacted = _SECRET_PATTERN.sub(r"\1[REDACTED]", redacted)
    return " ".join(redacted.strip().split())[:500]


class DoorDashCLIClient:
    """Run dd-cli arguments without exposing a shell or credentials."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        runner: CommandRunner | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        configured_path = executable or os.getenv("AIOS_DOORDASH_CLI_PATH")
        self.executable = (
            os.path.expanduser(configured_path)
            if configured_path
            else resolve_dd_cli_executable()
        )
        self._runner = runner or _default_runner
        self.timeout_seconds = timeout_seconds

    def _base_command(self) -> list[str]:
        if not self.executable:
            raise DoorDashCLIError(
                "The DoorDash CLI is not installed or configured",
                code="doordash_cli_unavailable",
                status_code=503,
            )
        return [self.executable]

    @staticmethod
    def _parse_arguments(arguments: str) -> list[str]:
        normalized = arguments.strip()
        if not normalized:
            raise DoorDashCLIError(
                "arguments is required",
                code="invalid_arguments",
                status_code=400,
            )
        if len(normalized) > MAX_ARGUMENTS_LENGTH:
            raise DoorDashCLIError(
                "arguments is too long",
                code="invalid_arguments",
                status_code=400,
            )
        try:
            parsed = shlex.split(normalized, posix=True)
        except ValueError as exc:
            raise DoorDashCLIError(
                "arguments contains invalid shell-style quoting",
                code="invalid_arguments",
                status_code=400,
            ) from exc
        if not parsed:
            raise DoorDashCLIError(
                "arguments is required",
                code="invalid_arguments",
                status_code=400,
            )
        if parsed[0] == "dd-cli":
            raise DoorDashCLIError(
                "Pass only the arguments that come after dd-cli",
                code="invalid_arguments",
                status_code=400,
            )
        if parsed[0] == "--json-output":
            parsed = parsed[1:]
        if not parsed:
            raise DoorDashCLIError(
                "A DoorDash CLI command is required",
                code="invalid_arguments",
                status_code=400,
            )
        if "--json-output" in parsed:
            raise DoorDashCLIError(
                "--json-output may only appear before the DoorDash command",
                code="invalid_arguments",
                status_code=400,
            )
        if parsed[0] == "login":
            raise DoorDashCLIError(
                "Use the DoorDash connection route for login",
                code="login_requires_connection_route",
                status_code=400,
            )
        if "--beautify" in parsed:
            raise DoorDashCLIError(
                "--beautify is incompatible with structured MCP output",
                code="invalid_arguments",
                status_code=400,
            )
        return parsed

    async def run_cli(self, arguments: str) -> dict[str, Any]:
        """Run the text that would follow `dd-cli`, always requesting JSON."""

        parsed = self._parse_arguments(arguments)
        result = await self._runner(
            [*self._base_command(), "--json-output", *parsed],
            self.timeout_seconds,
        )
        if result.returncode != 0:
            self._raise_command_error(result)
        if len(result.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise DoorDashCLIError(
                "The DoorDash CLI returned more data than mini-AIOS can safely load",
                code="doordash_response_too_large",
                status_code=502,
            )

        output = result.stdout.strip()
        if not output:
            return {"ok": True}
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise DoorDashCLIError(
                "The DoorDash CLI returned an invalid structured response",
                code="doordash_invalid_response",
                status_code=502,
            ) from exc
        if isinstance(payload, dict):
            return payload
        return {"result": payload}

    @staticmethod
    def _raise_command_error(result: CommandResult) -> None:
        details = _safe_error_text(result.stderr or result.stdout)
        lowered = details.lower()
        if any(
            marker in lowered
            for marker in (
                "not logged in",
                "login required",
                "run `dd-cli login`",
                "run dd-cli login",
                "unauthorized",
                "authentication",
                "expired",
            )
        ):
            raise DoorDashCLIError(
                "DoorDash needs to be connected again with dd-cli login",
                code="doordash_unauthorized",
                status_code=401,
            )
        suffix = f": {details}" if details else ""
        raise DoorDashCLIError(
            f"The DoorDash CLI command failed{suffix}",
            code="doordash_command_failed",
            status_code=502,
        )

    async def login(self) -> None:
        result = await self._runner(
            [*self._base_command(), "login"],
            LOGIN_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            self._raise_command_error(result)
