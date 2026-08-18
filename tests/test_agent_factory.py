import importlib

from aios_core.agent import (
    DEFAULT_MODEL_ID,
    DEFAULT_REASONING_EFFORT,
    create_main_agent,
    create_subagent_worker,
)
from aios_core.agent.factory import _resolve_model_configuration


def test_agent_factory_wraps_tools_and_prevents_recursive_subagents() -> None:
    main = create_main_agent(chat_id="chat-1")
    worker = create_subagent_worker(chat_id="chat-1")

    assert main.model == DEFAULT_MODEL_ID
    assert worker.model == DEFAULT_MODEL_ID
    if DEFAULT_REASONING_EFFORT is None:
        assert main.model_settings.reasoning is None
        assert worker.model_settings.reasoning is None
    else:
        assert main.model_settings.reasoning is not None
        assert worker.model_settings.reasoning is not None
        assert main.model_settings.reasoning.effort == DEFAULT_REASONING_EFFORT
        assert worker.model_settings.reasoning.effort == DEFAULT_REASONING_EFFORT

    main_tools = {tool.name: tool for tool in main.tools}
    worker_tools = {tool.name: tool for tool in worker.tools}
    assert "subagent" in main_tools
    assert "subagent" not in worker_tools
    assert "memory" in main_tools
    assert "memory" not in worker_tools
    assert "project" in main_tools
    assert "project" not in worker_tools
    assert '"project": (' in main.instructions
    assert '"project": (' not in worker.instructions
    assert "app_create" not in main_tools
    assert "apps_list" not in main_tools
    project_schema = main_tools["project"].params_json_schema
    assert set(project_schema["properties"]) == {"action", "project_id", "name"}
    assert project_schema["properties"]["action"]["enum"] == [
        "create",
        "get",
        "list",
        "update",
        "delete",
    ]

    for tool in main.tools:
        assert "fc" not in tool.params_json_schema.get("properties", {})

    assert "process_spawn" not in main_tools
    assert all(tool.strict_json_schema is True for tool in main.tools)


def test_default_model_configuration_is_gpt_5_6_xhigh() -> None:
    assert _resolve_model_configuration({}) == ("gpt-5.6", "xhigh")
    assert _resolve_model_configuration({"AIOS_MODEL_ID": "gpt-4.1"}) == (
        "gpt-4.1",
        None,
    )
    assert _resolve_model_configuration(
        {
            "AIOS_MODEL_ID": "gpt-5.6-terra",
            "AIOS_REASONING_EFFORT": "high",
        }
    ) == ("gpt-5.6-terra", "high")


def test_cron_tool_resolves_the_domain_manager_after_package_move() -> None:
    from aios_core import crons

    cron_tool = importlib.import_module("aios_core.agent.tools.cron")

    assert cron_tool._get_cron_manager() is crons.cron_manager
