from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from aios_core.integrations.doordash import (
    DoorDashConfig,
    DoorDashConnectionService,
    DoorDashConnectionStore,
)
from aios_core.integrations.doordash_mcp import (
    LocalDoorDashMCPTools,
    doordash_server_parameters,
    get_doordash_mcp_toolkit,
)
from aios_core.mcp_servers.doordash.client import (
    CommandResult,
    DoorDashCLIClient,
    DoorDashCLIError,
)

INTENT = (
    "Summary: Help the user order dinner\n"
    'user prompt/purpose: "Please find me enchiladas"'
)


class RecordingRunner:
    def __init__(
        self,
        results: list[CommandResult] | None = None,
    ) -> None:
        self.results = results or [
            CommandResult(0, '{"results": []}', ""),
        ]
        self.commands: list[tuple[list[str], float]] = []

    async def __call__(
        self,
        command: Sequence[str],
        timeout_seconds: float,
    ) -> CommandResult:
        self.commands.append((list(command), timeout_seconds))
        return self.results.pop(0)


def test_run_cli_parses_the_text_after_dd_cli_without_a_shell() -> None:
    runner = RecordingRunner()
    client = DoorDashCLIClient(
        executable="/fake/dd-cli",
        runner=runner,
    )

    result = asyncio.run(
        client.run_cli(
            "search --query 'enchiladas verdes' --limit 5 "
            f"--intent '{INTENT}'"
        )
    )

    assert result == {"results": []}
    assert runner.commands == [
        (
            [
                "/fake/dd-cli",
                "--json-output",
                "search",
                "--query",
                "enchiladas verdes",
                "--limit",
                "5",
                "--intent",
                INTENT,
            ],
            90.0,
        )
    ]


def test_shell_metacharacters_are_passed_as_plain_cli_arguments() -> None:
    runner = RecordingRunner()
    client = DoorDashCLIClient(
        executable="/fake/dd-cli",
        runner=runner,
    )

    asyncio.run(client.run_cli("search --query tacos ; touch /tmp/not-created"))

    assert runner.commands[0][0] == [
        "/fake/dd-cli",
        "--json-output",
        "search",
        "--query",
        "tacos",
        ";",
        "touch",
        "/tmp/not-created",
    ]


def test_json_output_is_added_once_and_beautify_is_rejected() -> None:
    runner = RecordingRunner()
    client = DoorDashCLIClient(
        executable="/fake/dd-cli",
        runner=runner,
    )

    asyncio.run(
        client.run_cli("--json-output address list --intent 'safe purpose'")
    )

    assert runner.commands[0][0].count("--json-output") == 1
    with pytest.raises(DoorDashCLIError) as error:
        asyncio.run(client.run_cli("address list --beautify"))
    assert error.value.code == "invalid_arguments"


@pytest.mark.parametrize("arguments", ["login", "--json-output login"])
def test_login_stays_on_the_separate_connection_rail(arguments: str) -> None:
    runner = RecordingRunner()
    client = DoorDashCLIClient(
        executable="/fake/dd-cli",
        runner=runner,
    )

    with pytest.raises(DoorDashCLIError) as error:
        asyncio.run(client.run_cli(arguments))

    assert error.value.code == "login_requires_connection_route"
    assert runner.commands == []


def test_login_uses_the_only_non_json_command() -> None:
    runner = RecordingRunner([CommandResult(0, "Logged in", "")])
    client = DoorDashCLIClient(
        executable="/fake/dd-cli",
        runner=runner,
    )

    asyncio.run(client.login())

    assert runner.commands == [
        (["/fake/dd-cli", "login"], 600.0),
    ]


def test_order_submit_is_forwarded_once_without_automatic_retry() -> None:
    runner = RecordingRunner(
        [CommandResult(0, '{"order_uuid": "order-1"}', "")]
    )
    client = DoorDashCLIClient(
        executable="/fake/dd-cli",
        runner=runner,
    )

    result = asyncio.run(
        client.run_cli(
            "order submit --cart-uuid cart-1 --tip-cents 500 --yes "
            f"--intent '{INTENT}'"
        )
    )

    assert result == {"order_uuid": "order-1"}
    assert len(runner.commands) == 1
    assert runner.commands[0][0] == [
        "/fake/dd-cli",
        "--json-output",
        "order",
        "submit",
        "--cart-uuid",
        "cart-1",
        "--tip-cents",
        "500",
        "--yes",
        "--intent",
        INTENT,
    ]


def test_cli_errors_are_stable_and_do_not_echo_tokens() -> None:
    runner = RecordingRunner(
        [
            CommandResult(
                1,
                "",
                "Authorization: Bearer access-secret is unauthorized",
            )
        ]
    )
    client = DoorDashCLIClient(
        executable="/fake/dd-cli",
        runner=runner,
    )

    with pytest.raises(DoorDashCLIError) as error:
        asyncio.run(client.run_cli("address list --intent 'safe purpose'"))

    assert error.value.code == "doordash_unauthorized"
    assert error.value.status_code == 401
    assert "access-secret" not in str(error.value)


def test_generic_cli_error_details_are_redacted() -> None:
    runner = RecordingRunner(
        [
            CommandResult(
                1,
                "",
                "upstream failed; access_token=access-secret",
            )
        ]
    )
    client = DoorDashCLIClient(
        executable="/fake/dd-cli",
        runner=runner,
    )

    with pytest.raises(DoorDashCLIError) as error:
        asyncio.run(client.run_cli("address list --intent 'safe purpose'"))

    assert error.value.code == "doordash_command_failed"
    assert "access-secret" not in str(error.value)
    assert "[REDACTED]" in str(error.value)


def test_connection_service_stores_state_but_not_credentials(tmp_path) -> None:
    runner = RecordingRunner([CommandResult(0, "Logged in", "")])
    client = DoorDashCLIClient(
        executable="/fake/dd-cli",
        runner=runner,
    )
    store = DoorDashConnectionStore(
        owner_id="owner-a",
        db_path=str(tmp_path / "aios.db"),
    )
    service = DoorDashConnectionService(
        owner_id="owner-a",
        config=DoorDashConfig(executable="/fake/dd-cli"),
        store=store,
        client=client,
    )

    before = service.connection_status()
    connected = asyncio.run(service.connect())
    disconnected = asyncio.run(service.disconnect())

    assert before["status"] == "disconnected"
    assert before["toolAvailable"] is True
    assert connected["connected"] is True
    assert connected["credentialOwner"] == "dd-cli"
    assert connected["credentialDelivery"] == "operating-system-keychain"
    assert disconnected["credentialsRemoved"] is False
    assert service.connection_status()["connected"] is False
    assert not hasattr(store.load(), "token")


def test_installed_cli_registers_mcp_without_duplicate_connection_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DoorDashConfig(executable="/fake/dd-cli")
    monkeypatch.setattr(
        DoorDashConfig,
        "from_env",
        staticmethod(lambda: config),
    )

    toolkit = get_doordash_mcp_toolkit()

    assert isinstance(toolkit, LocalDoorDashMCPTools)
    assert toolkit.include_tools == ["run_cli"]


def test_missing_or_disabled_cli_does_not_register_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for config in (
        DoorDashConfig(executable=None),
        DoorDashConfig(enabled=False, executable="/fake/dd-cli"),
    ):
        monkeypatch.setattr(
            DoorDashConfig,
            "from_env",
            staticmethod(lambda config=config: config),
        )
        assert get_doordash_mcp_toolkit() is None


def test_stdio_server_advertises_one_generic_cli_tool() -> None:
    async def list_tools():
        config = DoorDashConfig(executable="/fake/dd-cli")
        async with (
            stdio_client(doordash_server_parameters(config)) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            return (await session.list_tools()).tools

    tools = asyncio.run(list_tools())

    assert [tool.name for tool in tools] == ["run_cli"]
    assert tools[0].annotations.readOnlyHint is False
    assert tools[0].annotations.destructiveHint is True
    assert tools[0].annotations.idempotentHint is False


def test_agno_adapter_registers_one_prefixed_tool() -> None:
    async def connect_toolkit():
        toolkit = LocalDoorDashMCPTools(
            config=DoorDashConfig(executable="/fake/dd-cli")
        )
        try:
            await toolkit.connect()
            return (
                toolkit._initialized,
                sorted(toolkit.functions),
            )
        finally:
            await toolkit.close()

    initialized, tools = asyncio.run(connect_toolkit())

    assert initialized is True
    assert tools == ["doordash_run_cli"]
