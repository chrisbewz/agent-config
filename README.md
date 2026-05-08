# agent-config

A Python CLI tool for deploying and managing AI coding agent configurations —
MCP servers, instruction files (AGENTS.md, CLAUDE.md), skills, and runtime memory —
across multiple providers and machines.

Keep your configs in version control. Run `agent-config deploy` to push them to every tool.

## Install

```sh
pip install agentcfg
```

Or with uv:

```sh
uv tool install agentcfg
```

## Quick Start

```powershell
# Scaffold a new config directory
agent-config init

# Edit the generated files to match your setup
# (mcp_providers.json, agents.json, instruction_providers.json …)

# Deploy everything
agent-config deploy
```

## Commands

| Command | Description |
|---|---|
| `agent-config init` | Scaffold a new config repo with starter templates |
| `agent-config deploy` | Deploy manifest entries (files/dirs) to their destinations |
| `agent-config pull` | Pull memory files back from deployed locations into the repo |
| `agent-config sync` | Deploy then run `agents sync` across all LLM providers |
| `agent-config mcp deploy` | Write MCP server configs to all registered provider files |
| `agent-config mcp sync` | Additive MCP sync — add only servers missing from disk |
| `agent-config mcp show` | Show MCP configuration status per provider |
| `agent-config instructions deploy` | Copy instruction files (AGENTS.md etc.) to each provider |
| `agent-config instructions show` | Show instruction file status per provider |
| `agent-config skills deploy` | Deploy all registered skills to their target agents |
| `agent-config skills sync` | Deploy only skills not yet installed |
| `agent-config skills show` | Show skill deployment status |
| `agent-config providers list` | List registered MCP providers |
| `agent-config providers add` | Add a new MCP provider to the registry |
| `agent-config providers remove` | Remove an MCP provider |
| `agent-config providers set-path` | Update a provider's config file path |

### Global options

| Flag | Description |
|---|---|
| `--config-dir DIR` / `-C DIR` | Root of your config repo (default: cwd or `$AGENT_CONFIG_DIR`) |
| `--version` / `-V` | Print version and exit |

### Filtering and dry-run

Most commands accept `--providers p1 p2 …` to limit to specific providers,
and `--dry-run` to preview changes without writing anything.

## Config Files

| File | Purpose |
|---|---|
| `manifest.json` | Maps source files/dirs to their deploy destinations |
| `agents.json` | MCP server definitions and integration toggles |
| `mcp_providers.json` | Registry of MCP provider config paths and formats |
| `instruction_providers.json` | Registry of instruction file destinations per provider |
| `skills_registry.json` | Registry of skills (local directories or skills-cli packages) |

## Prerequisites

| Tool | Purpose |
|---|---|
| [Python 3.8+](https://www.python.org/downloads/) | Runtime for `agent-config` |
| [uv](https://docs.astral.sh/uv/) | Package manager |
| [just](https://github.com/casey/just) | Task runner (optional, for `justfile` recipes) |
| [Node.js + npx](https://nodejs.org) | Required only for `installer: skills-cli` manifest entries |
| [Git](https://git-scm.com/) | Required for `pull` command (`git add`) and submodules |

## Environment Variables

| Variable | Description |
|---|---|
| `AGENT_CONFIG_DIR` | Override the default config directory (equivalent to `--config-dir`) |

## Supported MCP Providers

Out of the box: **VS Code**, **GitHub Copilot CLI**, **Claude Desktop**, **OpenCode**,
**Cursor**, **Windsurf**, **JetBrains Junie**.

Add custom providers with `agent-config providers add`.

## Cross-Platform

Works on Windows (PowerShell), macOS, and Linux. Path expansion supports both
`%USERPROFILE%` (Windows) and `~` / `$HOME` (POSIX).
