#!/usr/bin/env bash
# Smoke tests for install-copilot-agent.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="${SCRIPT_DIR}/../install-copilot-agent.sh"

PASS=0
FAIL=0
TEST_BIN_DIR="$(mktemp -d)"
TEST_HOME_DIR="${TEST_BIN_DIR}/home"
TEST_HOME_BIN="${TEST_HOME_DIR}/.local/bin"
mkdir -p "${TEST_HOME_BIN}"

cleanup() {
    rm -rf "$TEST_BIN_DIR"
}
trap cleanup EXIT

echo ""
echo "Running install-copilot-agent.sh smoke tests..."
echo ""

unknown_out="$(
    bash "$INSTALLER" --bogus 2>&1 || true
)"
if printf '%s' "$unknown_out" | grep -q "Unknown argument"; then
    echo "  PASS  unknown argument fails closed"
    ((PASS++)) || true
else
    echo "  FAIL  unknown argument did not return the expected error"
    ((FAIL++)) || true
fi

# Stub claude/coprocess detection so --status does not depend on the user's
# real local setup and cannot block on a slow `claude mcp get`.
cat > "${TEST_HOME_BIN}/claude" <<'FAKE_CLAUDE'
#!/usr/bin/env bash
if [[ "${1:-}" == "mcp" && "${2:-}" == "get" ]]; then
    echo "no mcp server found"
    exit 0
fi
if [[ "${1:-}" == "mcp" && "${2:-}" == "list" ]]; then
    exit 0
fi
exit 0
FAKE_CLAUDE
chmod +x "${TEST_HOME_BIN}/claude"

cat > "${TEST_HOME_BIN}/copilot" <<'FAKE_COPILOT'
#!/usr/bin/env bash
exit 0
FAKE_COPILOT
chmod +x "${TEST_HOME_BIN}/copilot"

status_rc=0
HOME="${TEST_HOME_DIR}" PATH="/usr/bin:/bin" bash "$INSTALLER" --status >/dev/null 2>&1 || status_rc=$?
if [[ "$status_rc" -eq 0 ]]; then
    echo "  PASS  --status exits successfully"
    ((PASS++)) || true
else
    echo "  FAIL  --status failed (rc=$status_rc)"
    ((FAIL++)) || true
fi

drift_install_dir="${TEST_HOME_DIR}/.local/share/ai-agent/copilot"
mkdir -p "${drift_install_dir}/mcp"
printf '%s\n' '# stale installed copy' > "${drift_install_dir}/mcp/copilot_mcp_server.py"

drift_status_out="$(
    HOME="${TEST_HOME_DIR}" PATH="/usr/bin:/bin" bash "$INSTALLER" --status 2>&1 || true
)"
if printf '%s' "$drift_status_out" | grep -q "Repo sync:.*UPDATE NEEDED"; then
    echo "  PASS  --status reports repo/install drift"
    ((PASS++)) || true
else
    echo "  FAIL  --status did not report repo/install drift"
    ((FAIL++)) || true
fi

# Partial claude output + exit 124 should prefer the parsed status over a generic TIMEOUT.
cat > "${TEST_HOME_BIN}/claude" <<'FAKE_CLAUDE_TIMEOUT'
#!/usr/bin/env bash
if [[ "${1:-}" == "mcp" && "${2:-}" == "get" ]]; then
    echo "copilot-delegate:"
    echo "  Status: ✗ Failed to connect"
    exit 124
fi
if [[ "${1:-}" == "mcp" && "${2:-}" == "list" ]]; then
    exit 0
fi
exit 0
FAKE_CLAUDE_TIMEOUT
chmod +x "${TEST_HOME_BIN}/claude"

status_out="$(
    HOME="${TEST_HOME_DIR}" PATH="/usr/bin:/bin" bash "$INSTALLER" --status 2>&1 || true
)"
if printf '%s' "$status_out" | grep -q "REGISTERED BUT NOT CONNECTING" && \
   ! printf '%s' "$status_out" | grep -q "MCP registered: TIMEOUT"; then
    echo "  PASS  --status prefers parsed MCP error over generic timeout"
    ((PASS++)) || true
else
    echo "  FAIL  --status misclassified partial claude output"
    ((FAIL++)) || true
fi

# Registration should tolerate a slow `claude mcp get` probe and invoke a
# correctly formed `claude mcp add` command without duplicating the `add` verb.
cat > "${TEST_HOME_BIN}/claude" <<'FAKE_CLAUDE_REGISTER'
#!/usr/bin/env bash
if [[ "${1:-}" == "mcp" && "${2:-}" == "get" ]]; then
    sleep "${FAKE_CLAUDE_GET_DELAY:-0}"
    echo "no mcp server found"
    exit 0
fi
if [[ "${1:-}" == "mcp" && "${2:-}" == "add" ]]; then
    printf '%s\n' "$*" > "${CLAUDE_ARGS_FILE:?}"
    exit 0
fi
if [[ "${1:-}" == "mcp" && "${2:-}" == "remove" ]]; then
    exit 0
fi
exit 0
FAKE_CLAUDE_REGISTER
chmod +x "${TEST_HOME_BIN}/claude"

register_args_file="$(mktemp)"
register_rc=0
HOME="${TEST_HOME_DIR}" \
PATH="/usr/bin:/bin" \
TEST_INSTALLER="$INSTALLER" \
TEST_BIN_DIR="$TEST_BIN_DIR" \
TEST_HOME_DIR="$TEST_HOME_DIR" \
CLAUDE_ARGS_FILE="$register_args_file" \
FAKE_CLAUDE_GET_DELAY=2 \
SKIP_CLAUDE_REGISTRATION=0 \
bash -c '
    set -euo pipefail
    export INSTALLER_LIB_ONLY=1
    source "$TEST_INSTALLER"
    PATH="$HOME/.local/bin:/usr/bin:/bin"
    INSTALL_DIR="$TEST_BIN_DIR/install-root"
    VENV_DIR="${INSTALL_DIR}/.venv"
    MCP_SERVER="${INSTALL_DIR}/mcp/copilot_mcp_server.py"
    COPILOT_CLI_PATH="$HOME/.local/bin/copilot"
    PYTHON_BIN="$(command -v python3)"
    CLAUDE_CMD_TIMEOUT=1
    register_claude_mcp >/dev/null
' >/dev/null 2>&1 || register_rc=$?

register_args="$(cat "$register_args_file" 2>/dev/null || true)"
rm -f "$register_args_file"
if [[ "$register_rc" -eq 0 ]] && \
   printf '%s' "$register_args" | grep -q "mcp add -s user copilot-delegate -e COPILOT_INSTALL_DIR=" && \
   printf '%s' "$register_args" | grep -q " -e COPILOT_BIN=" && \
   printf '%s' "$register_args" | grep -q " -- .*\\.venv/bin/python .*copilot_mcp_server.py" && \
   ! printf '%s' "$register_args" | grep -q "mcp add add"; then
    echo "  PASS  registration tolerates slow claude probe and uses correct add args"
    ((PASS++)) || true
else
    echo "  FAIL  registration command was malformed or hung (rc=$register_rc)"
    ((FAIL++)) || true
fi

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
echo ""
[[ "$FAIL" -eq 0 ]]
