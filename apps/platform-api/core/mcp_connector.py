def discover_mcp_tools(context):
    """Return the tool descriptors that the configured MCP server exposes."""

    endpoint, _, timeout_seconds = _mcp_config(context)
    headers = _resolve_auth_headers(context)
    with McpClient(endpoint, headers, timeout_seconds) as client:
        client.initialize()
        return client.list_tools()
