# Copilot MCP server for Claude

An MCP (Model Context Protocol) server that lets Claude Code delegate developer tasks to GitHub Copilot CLI. Tasks are routed to specialized agents based on config-driven profiles — each with its own model, prompt, tool whitelist, and safety rules.

## Architecture

```
Claude Code
  ├── run_agent_simple        (gpt-4o-mini) — code, boilerplate, refactors
  ├── run_agent_security      (gpt-4o)      — security analysis (read-only)
  └── run_agent_code_review   (gpt-4o)      — code quality review (read-only)
        ↑ dynamically registered from config.yaml at MCP server startup

Project-level Claude subagents
  ├── copilotsimple           → proactively uses run_agent_simple when appropriate
  ├── copilotsecurity         → proactively uses run_agent_security when appropriate
  └── copilotcodereview       → proactively uses run_agent_code_review when appropriate

copilot_mcp_server.py
  └── reads config.yaml → registers one MCP tool per profile
        └── copilot_wrapper.sh [--model MODEL] [--prompt-prefix PREFIX]
                               [--allowed-tool TOOL ...] TASK
              └── copilot CLI --available-tools=<whitelist> --allow-all-tools
```

## Requirements

- **Python 3.11+** (auto-installed by the installer if missing)
- **GitHub Copilot CLI** (`copilot`) in PATH or set via `COPILOT_BIN`
- **Claude Code** CLI for automatic MCP registration (optional — can register manually)
- **make** for the bundled developer shortcuts in `Makefile` (available by default on Linux and macOS)

## Quick Start

```bash
chmod +x install-copilot-agent.sh
./install-copilot-agent.sh
```

The installer will:

1. Find or install Python 3.11+
2. Create a virtualenv with the MCP SDK (`mcp[cli]>=1.20`), PyYAML, and Trio
3. Deploy the MCP server, wrapper, redaction module, and default `config.yaml`
4. Register the `copilot-delegate` MCP server with Claude Code

If you do a system-wide install with `sudo INSTALL_DIR=/opt/...`, the installer skips Claude registration on purpose and prints the exact `claude mcp add ...` command to run later as the real user.

## File Structure

```
copilot/                          ← this repository
├── Makefile                      ← common dev/test/install commands
├── install-copilot-agent.sh      ← installer / updater
├── config.yaml                   ← default profiles
├── pyproject.toml                ← project metadata and test dependencies
├── CLAUDE.md                     ← Claude routing instructions
├── .claude/agents/               ← project-level Claude subagents
├── src/
│   ├── copilot_mcp_server.py     ← MCP server (dynamic tool registration)
│   ├── copilot_wrapper.sh        ← safety wrapper for Copilot CLI
│   └── redact.py                 ← shared secret-redaction module
└── tests/
    ├── test_installer.sh         ← installer smoke tests (bash)
    ├── test_wrapper.sh           ← wrapper smoke tests (bash)
    └── test_mcp_server.py        ← unit tests for server logic (pytest)
```

After installation:

```
~/.local/share/ai-agent/copilot/
├── bin/
│   ├── copilot_wrapper.sh
│   └── redact.py                 ← used by the wrapper at runtime
├── mcp/
│   ├── copilot_mcp_server.py
│   └── redact.py                 ← used by the server at runtime
├── config.yaml                   ← edit this to customize profiles
├── logs/copilot-mcp.log
└── .venv/
```

## Configuration

Edit `~/.local/share/ai-agent/copilot/config.yaml` to customize or add profiles:

```yaml
profiles:
  simple:
    model: gpt-4o-mini
    description: "Delegate simple developer tasks..."
    prompt_prefix: "Respond concisely..."
    timeout: 300               # seconds
    max_input_length: 5000     # characters
    max_output_length: 16000   # characters (output never exceeds this limit)
    allowed_tools:             # tools Copilot is permitted to use
      - view
      - show_file
      - create
      - edit
      - grep
      - glob
      - bash
      - web_fetch
    blocked_patterns:
      - "rm -rf"
      - "mkfs"
    rejected_keywords:
      - architecture
      - security

  my_custom_profile:
    model: gpt-4o
    description: "Use for X, Y, Z tasks."
    prompt_prefix: "You are an expert in..."
    timeout: 600
    max_input_length: 30000
    max_output_length: 15000
    allowed_tools:
      - view
      - show_file
      - grep
      - glob
      - web_fetch
    blocked_patterns: []
    rejected_keywords: []
```

### Profile fields

| Field | Type | Default | Description |
|---|---|---|---|
| `model` | string | `gpt-4o-mini` | Copilot model to use |
| `description` | string | — | Shown to Claude as the tool description |
| `prompt_prefix` | string | — | Prepended to every task prompt |
| `timeout` | int | `300` | Max seconds Copilot may run |
| `max_input_length` | int | `5000` | Max task length in characters |
| `max_output_length` | int | `16000` | Max output length in characters (hard limit — truncation marker included within) |
| `allowed_tools` | list | built-in profile default / `[]` for custom profiles | Copilot tools the model may use (see below) |
| `blocked_patterns` | list | `[]` | Task substrings that are rejected outright |
| `rejected_keywords` | list | `[]` | Task keywords that route back to Claude |

All fields are optional — missing fields fall back to safe defaults. Malformed profiles (wrong type, non-string key) are skipped with a warning; if no valid profile survives, the server starts with the built-in `simple` default.

### Tool whitelist (`allowed_tools`)

Each profile can declare exactly which Copilot tools the model is allowed to use. Only listed tools are visible to the model — tools outside the list are completely unavailable for that profile.

Available tools (from `copilot --help`):

| Tool | Category | Description |
|---|---|---|
| `view`, `show_file` | Read | View file contents |
| `create`, `edit` | Write | Create / modify files |
| `grep`, `glob` | Search | Search code and find files |
| `bash` | Shell | Execute shell commands |
| `web_fetch` | Web | Fetch URLs |
| `sql` | Database | SQLite queries |

**`allowed_tools` semantics:**

| Config value | Effect |
|---|---|
| Field absent (or invalid type) | Inherit built-in defaults for known profiles; disable all tools for custom profiles |
| `allowed_tools: []` | Explicitly disable all Copilot tools for this profile |
| `allowed_tools: [view, grep]` | Allow exactly the listed tools |

Items with commas (e.g. `"view,bash"`) are rejected at load time with a warning — write each tool as a separate list entry.

**Default per-profile whitelist:**

| Profile | Write | Shell | Reason |
|---|---|---|---|
| `simple` | ✓ `create`, `edit` | ✓ `bash` | Needs to write code and run tests/build |
| `security` | ✗ | ✗ | Read-only — analysis must not modify files |
| `code_review` | ✗ | ✗ | Read-only — review must not modify files |

Built-in profiles inherit their built-in whitelist even if you only override some of their fields.
Custom profiles default to no Copilot tools until you list them explicitly.

After editing config, run:

```bash
./install-copilot-agent.sh --update
```

Then restart Claude Code — it will pick up the new tool list automatically.

## Automatic Claude Subagents

This repository also includes project-level Claude Code subagents in `.claude/agents/`.
They are separate from MCP tools:

- MCP tools are the callable functions exposed by `copilot-delegate`
- Claude subagents are project-local specialist roles that Claude can choose automatically

The included subagents are configured to proactively route matching tasks to:

- `copilotsimple` → `run_agent_simple`
- `copilotsecurity` → `run_agent_security`
- `copilotcodereview` → `run_agent_code_review`

Their names intentionally avoid hyphens so `@` mentions in Claude Code stay simple and usually do not need quoting.

Claude Code discovers project subagents automatically from `.claude/agents/`. If Claude is already open, restart the session after pulling changes.

## Development Workflow

The repository includes a `Makefile` so the most common local tasks are the same
on Linux and macOS:

```bash
# Show available targets
make help

# Prepare both virtualenvs used by the project
make bootstrap

# Run lint + all tests
make check

# Run the MCP server directly from the repo checkout
make run-server
```

Useful granular targets:

```bash
make venv
make test-venv
make lint
make test
make test-python
make test-shell
make test-wrapper
make test-installer
make clean
make distclean
```

## Commands

```bash
# Full install
./install-copilot-agent.sh

# Update files and dependencies (after editing config or src/)
./install-copilot-agent.sh --update

# Check installation status + recent logs
./install-copilot-agent.sh --status
```

The same installer actions are also available through `make`:

```bash
make install
make update
make status
```

`--update` redeploys source files and updates Python dependencies in the virtualenv (same as a fresh install, but skips OS-level Python installation).

Example system-wide install:

```bash
sudo INSTALL_DIR=/opt/ai-agent/copilot ./install-copilot-agent.sh
```

## Optional Environment Variables

| Variable | Default | Description |
|---|---|---|
| `INSTALL_DIR` | `~/.local/share/ai-agent/copilot` | Installer target directory |
| `COPILOT_BIN` | auto-detected | Path to Copilot CLI binary |
| `COPILOT_MODEL` | `gpt-4o-mini` | Default model (overridden by profile `model` field) |
| `COPILOT_INSTALL_DIR` | `~/.local/share/ai-agent/copilot` | Runtime directory (MCP server + wrapper use this) |
| `COPILOT_SEMAPHORE_TIMEOUT` | `30` | Max seconds to wait for a concurrency slot before returning "server busy" |
| `COPILOT_LOG_FORMAT` | `text` | Log format: `text` (human-readable) or `json` (one JSON object per line) |

> `INSTALL_DIR` is used only by the installer. At runtime, the MCP server and wrapper read `COPILOT_INSTALL_DIR`. The installer automatically wires them up via `-e COPILOT_INSTALL_DIR=...` in the `claude mcp add` command.

## Supported Platforms

| Platform | Package manager | Notes |
|---|---|---|
| Debian / Ubuntu | apt | Tested |
| Fedora | dnf | Tested |
| RHEL / CentOS / Rocky / Alma | dnf / yum | Tested |
| Arch / Manjaro | pacman | Tested |
| openSUSE / SUSE | zypper | Tested |
| macOS (Intel + Apple Silicon) | Homebrew | Compatible — see note below |

> **macOS:** The installer extends `PATH` with `/opt/homebrew/bin` (Apple Silicon) and `/usr/local/bin` (Intel) before searching for Python or Copilot, so it works in non-interactive shells where the user's shell profile has not been sourced. The wrapper uses portable BSD-compatible commands (`mktemp`, `date`, `tr`). Not tested on a physical Mac — if you hit an issue, please [open an issue](../../issues).

## Safety Features

**Wrapper exit codes:**

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Missing or empty task / unknown option |
| `2` | Task exceeds `max_input_length` |
| `3` | Control characters detected in task |
| `4` | Task matches a `blocked_pattern` |
| `5` | Copilot CLI exited with a non-zero code or timed out |
| `6` | Copilot CLI binary not found or not executable |
| `7` | Python not available (required for task normalization and output processing) |

**Output sanitization** — secrets are redacted before the response reaches Claude. The redaction module (`src/redact.py`) is the single source of truth shared by both the server and the wrapper, so the two never drift apart:

| Pattern | Examples |
|---|---|
| GitHub PATs | `ghp_…`, `github_pat_…`, `gho_…` |
| OpenAI / Anthropic keys | `sk-…`, `sk-ant-…` |
| Stripe keys | `sk_live_…`, `pk_live_…` |
| AWS access key IDs | `AKIA…` |
| Slack tokens | `xoxb-…`, `xoxa-…`, `xoxp-…` |
| Google / GCP tokens | `AIzaSy…`, `ya29.…`, `eyJ…` (JWT / service account) |
| GitLab tokens | `glpat-…`, `gldt-…` |
| SendGrid API keys | `SG.…` |
| Supabase keys | `sbp_…` |
| HTTP Bearer tokens | `Bearer <token>` (space or tab separator) |
| Azure Storage / SAS | `AccountKey=…`, `sig=…` |
| PEM private keys | `-----BEGIN … PRIVATE KEY-----` |
| Database URLs with credentials | `postgresql://user:pass@host/db` |
| Generic key assignments | `api_key=…`, `token=…`, `secret=…`, `password=…`, `passwd=…` |

Output is also truncated to `max_output_length` characters. When truncation occurs, a `...[truncated]` marker is appended; the total length (content + marker) never exceeds the configured limit.

**Unicode bypass prevention** — `rejected_keywords` matching is applied after:

1. NFKC normalization (collapses fullwidth Latin: `ｓｅｃｕｒｉｔｙ` → `security`)
2. Zero-width character removal (`se​curity` → `security`)
3. Cyrillic and Greek homoglyph mapping (`sеcurity` with Cyrillic е → `security`)

**Profile-level policy:**

- `blocked_patterns` — task substrings that cause immediate rejection (exit 4); matched case-insensitively after whitespace normalization
- `rejected_keywords` — task keywords that route the request back to Claude with an error (Unicode-normalized, case-insensitive)
- `allowed_tools` — tool whitelist resolved once at server startup and passed to Copilot via `--available-tools`; the model cannot invoke tools outside this list

**Concurrency:** at most 3 Copilot processes run in parallel (configurable via `COPILOT_SEMAPHORE_TIMEOUT`).

## Logs

Runtime logs: `~/.local/share/ai-agent/copilot/logs/copilot-mcp.log`

Each entry includes timestamp, profile name, and success/failure status. The log file rotates at 10 MB (previous file kept as `.log.1`).

### Structured JSON logging

Set `COPILOT_LOG_FORMAT=json` in the MCP server environment (or in `claude mcp add -e`) to emit one JSON object per line instead of plain text:

```json
{"ts": "2026-04-06T12:00:00", "profile": "simple", "msg": "success", "len": "1234"}
```

This format is easy to pipe into `jq`, aggregate in Loki/Grafana, or feed into audit pipelines.

## Running Tests

Preferred developer workflow:

```bash
make test-venv
make test
```

Full validation:

```bash
make check
```

Manual equivalents:

```bash
pip install -e ".[test]"
pytest
bash tests/test_wrapper.sh
bash tests/test_installer.sh
```

## Manual MCP Registration

The `-s user` flag registers the server user-wide (visible in all projects). Without it the default scope is `local`, which only applies to the current project directory.

```bash
claude mcp add -s user copilot-delegate \
  -e COPILOT_INSTALL_DIR="${HOME}/.local/share/ai-agent/copilot" \
  -e COPILOT_BIN="/path/to/copilot" \
  -- "${HOME}/.local/share/ai-agent/copilot/.venv/bin/python" \
     "${HOME}/.local/share/ai-agent/copilot/mcp/copilot_mcp_server.py"
```

To enable JSON logging:

```bash
claude mcp add -s user copilot-delegate \
  -e COPILOT_INSTALL_DIR="${HOME}/.local/share/ai-agent/copilot" \
  -e COPILOT_BIN="/path/to/copilot" \
  -e COPILOT_LOG_FORMAT=json \
  -- "${HOME}/.local/share/ai-agent/copilot/.venv/bin/python" \
     "${HOME}/.local/share/ai-agent/copilot/mcp/copilot_mcp_server.py"
```

## Uninstall

```bash
rm -rf ~/.local/share/ai-agent/copilot
claude mcp remove copilot-delegate
```

## License

MIT
