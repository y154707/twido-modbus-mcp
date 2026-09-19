# Twido PLC Modbus MCP Server

An MCP server to interact, back up, and safely map Schneider Twido PLCs using natural language in Cursor and GitHub Copilot.

## Quickstart Configuration

Ensure you have `uv` installed (`pip install uv` or `brew install uv`).

### VS Code / Copilot
Add to `.vscode/mcp.json`:
\`\`\`json
{
  "mcpServers": {
    "twido-mcp": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/YOUR_USERNAME/twido-modbus-mcp", "twido-mcp"]
    }
  }
}
\`\`\`

### Cursor
Go to Settings > MCP > Add New Server:
- **Type:** command
- **Command:** `uvx --from git+https://github.com/YOUR_USERNAME/twido-modbus-mcp twido-mcp`

## Example Prompts
- *"Read the current holding registers from the PLC at 192.168.1.10 starting at address 0."*
- *"Take a full backup of the PLC registers and save it to factory_backup.json."*
- *"I am standing next to the physical panel. Run a single test on output 2 with human confirmation."*