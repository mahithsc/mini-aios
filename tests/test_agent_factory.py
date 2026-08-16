from aios_core.agent import DEFAULT_MODEL_ID, create_main_agent, create_subagent_worker


def test_agent_factory_wraps_tools_and_prevents_recursive_subagents() -> None:
    main = create_main_agent(chat_id="chat-1")
    worker = create_subagent_worker(chat_id="chat-1")

    assert main.model == DEFAULT_MODEL_ID
    assert worker.model == DEFAULT_MODEL_ID

    main_tools = {tool.name: tool for tool in main.tools}
    worker_tools = {tool.name: tool for tool in worker.tools}
    assert "subagent" in main_tools
    assert "subagent" not in worker_tools
    assert "memory" in main_tools
    assert "memory" not in worker_tools

    for tool in main.tools:
        assert "fc" not in tool.params_json_schema.get("properties", {})

    # This one schema contains an arbitrary string map, which cannot use the
    # OpenAI strict-schema subset; every other tool remains strict.
    assert main_tools["process_spawn"].strict_json_schema is False
    assert main_tools["read"].strict_json_schema is True
