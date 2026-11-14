# Setting up plug-play-memory with Claude Desktop

## Prerequisites

1. Install plug-play-memory:
   ```bash
   pip install plug-play-memory
   ```

2. Verify it works:
   ```bash
   agentmemory doctor
   ```

## Step 1: Initialize a project

```bash
agentmemory init ~/my-agent --type coding
cd ~/my-agent
```

This creates:
- `config.yaml` — auth config with sensible defaults
- `policies/custom/` — your custom policy overrides (starts empty)
- `.gitignore` — keeps your memory.db out of git

## Step 2: Edit config.yaml (optional)

Open `~/my-agent/config.yaml` and customize:
- `default_agent_id` — what to call this agent (e.g., "claude-coder")
- `default_agent_type` — `coding`, `study`, `financial`, or `default`
- `default_scopes` — what this agent is allowed to do

For a coding assistant that can read/write memory but not delete:
```yaml
auth:
  enabled: true
  default_agent_id: claude-coder
  default_agent_type: coding
  default_scopes:
    - memory:read
    - memory:write
    - permission:check
    - admin:audit
```

## Step 3: Add to Claude Desktop config

Find your Claude Desktop config file:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

Edit it to add plug-play-memory (see `claude_desktop_config.json` in this folder).

**Important:** Use ABSOLUTE paths, not `~` or relative paths.

## Step 4: Restart Claude Desktop

Quit Claude Desktop completely (Cmd+Q on macOS) and reopen it.

## Step 5: Test it

In Claude Desktop, ask:

> "Remember that I prefer Python over JavaScript for backend work."

Claude will call `remember_event(action="fact_observed", ...)`. You can verify it was saved:

```bash
cd ~/my-agent
sqlite3 memory.db "SELECT id, tier, key, value FROM facts WHERE superseded = 0;"
```

You should see the fact in the `core` tier.

## Step 6: Use it in conversations

Now Claude has persistent memory across conversations. Try:

> "What programming language do I prefer?"

Claude will call `recall(query="programming language", tier="archival")` and find the fact you saved earlier.

## Troubleshooting

### "Server failed to start"
- Check the path in your config is absolute
- Check `agentmemory doctor` passes
- Try running `agentmemory serve` manually in a terminal — errors will show

### "Tool not found"
- Restart Claude Desktop completely (don't just close the window)
- Check the config JSON is valid (no trailing commas, proper brackets)

### "Permission denied" on every action
- Check your `default_scopes` in config.yaml includes the scope the tool needs
- See `auth.py` for the full list of scopes

## Multi-agent setup

If you want multiple agents (e.g., a coder and a study tutor), create multiple projects and add multiple MCP servers:

```json
{
  "mcpServers": {
    "memory-coder": {
      "command": "agentmemory",
      "args": ["serve", "--db", "/path/to/coder/memory.db", "--config", "/path/to/coder/config.yaml"],
      "env": {
        "AGENTMEMORY_AGENT_ID": "coder-1",
        "AGENTMEMORY_AGENT_TYPE": "coding"
      }
    },
    "memory-tutor": {
      "command": "agentmemory",
      "args": ["serve", "--db", "/path/to/tutor/memory.db", "--config", "/path/to/tutor/config.yaml"],
      "env": {
        "AGENTMEMORY_AGENT_ID": "priya-tutor",
        "AGENTMEMORY_AGENT_TYPE": "study"
      }
    }
  }
}
```

Each agent has its own memory file and its own policy pack.
