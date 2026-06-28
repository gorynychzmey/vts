from __future__ import annotations


async def test_server_registers_expected_tools() -> None:
    from vts.mcp.server import build_mcp_server

    mcp = build_mcp_server()
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    expected = {
        "submit_video",
        "list_tasks",
        "get_status",
        "get_transcript",
        "get_prompt_result",
        "list_prompts",
        "create_prompt",
        "update_prompt",
        "delete_prompt",
        "list_presets",
        "create_preset",
        "update_preset",
        "delete_preset",
        "get_default_preset",
        "set_default_preset",
        "wait_for_task",
    }
    assert expected.issubset(names), f"missing tools: {expected - names}"
    assert "get_summary" not in names
