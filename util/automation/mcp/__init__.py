"""MCP server wrapper around util.automation.

Exposes the entire toolkit (index, state, eye classification, replay
mutation, fix validation, shader indexing, …) as `@mcp.tool` functions
so LLM agents can drive it.

Run:
    python -m util.automation.mcp.server
"""
