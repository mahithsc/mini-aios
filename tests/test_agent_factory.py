from aios_core.agent import (
    DEFAULT_MODEL_ID,
    DEFAULT_REASONING_EFFORT,
    _resolve_model_configuration,
    create_main_agent,
    create_subagent_worker,
)


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
