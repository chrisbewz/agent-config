set shell := ["powershell.exe", "-c"]

# Deploy all manifest entries to their configured destinations
deploy:
    uv run agent_config.py deploy

# Preview deploy without making changes
deploy-dry:
    uv run agent_config.py deploy --dry-run

# Deploy MCP configs to all registered providers
mcp-deploy:
    uv run agent_config.py mcp deploy

# Preview MCP deploy without changes
mcp-deploy-dry:
    uv run agent_config.py mcp deploy --dry-run

# Additive MCP sync (add only missing servers)
mcp-sync:
    uv run agent_config.py mcp sync

# Show MCP configuration status across providers
mcp-show:
    uv run agent_config.py mcp show

# Deploy instruction files (AGENTS.md etc.) to all providers
instructions-deploy:
    uv run agent_config.py instructions deploy

# Show instruction file status
instructions-show:
    uv run agent_config.py instructions show

# Deploy all registered skills
skills-deploy:
    uv run agent_config.py skills deploy

# Preview skills deploy
skills-deploy-dry:
    uv run agent_config.py skills deploy --dry-run

# Show skills deployment status
skills-show:
    uv run agent_config.py skills show

# Pull memory files back from deployed locations into the repo
pull:
    uv run agent_config.py pull

# Full cycle: deploy then agents sync
sync:
    uv run agent_config.py sync

# Reinstall CLI from source after editing agent_config.py
reinstall:
    uv tool install . --reinstall
