#!/usr/bin/env bash
# =============================================================================
# AI Agent Copilot - Universal installer
#
# Installs a Copilot MCP agent for Claude Code on any machine.
# Supports Linux (Debian/Ubuntu, Fedora/RHEL, Arch, SUSE) and macOS.
#
# What it does:
#   1. Checks for / installs Python 3.11+
#   2. Creates a virtualenv with the MCP SDK
#   3. Deploys the MCP server, wrapper, and config
#   4. Registers the MCP server with Claude Code
#
# Usage:
#   chmod +x install-copilot-agent.sh && ./install-copilot-agent.sh
#   ./install-copilot-agent.sh --update   # update files only, skip Python/venv setup
#   ./install-copilot-agent.sh --status   # show installation status
#
# Optional environment variables:
#   INSTALL_DIR    - target directory (default: ~/.local/share/ai-agent/copilot)
#   COPILOT_BIN    - path to Copilot CLI binary (auto-detected)
#   COPILOT_MODEL  - default model (default: gpt-4o-mini)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Extend PATH with common Python / package-manager installation directories so
# that find_python and detect_copilot_cli work in non-interactive shells where
# the user's shell profile (e.g. .zshrc) has not been sourced.
#
# macOS: Homebrew on Apple Silicon installs to /opt/homebrew/bin; on Intel Macs
#        to /usr/local/bin.  Both are included so the installer works on either
#        architecture without an architecture check.
# Linux: ~/.local/bin covers pip --user installs; /usr/local/bin covers manual
#        or pyenv-style installs.
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:${PATH:-/usr/bin:/bin}"

# -- Configuration --
# Track whether the user explicitly passed INSTALL_DIR so load_install_dir()
# knows not to override an explicit choice with the saved state.
INSTALL_DIR_EXPLICIT="${INSTALL_DIR:+1}"
INSTALL_DIR="${INSTALL_DIR:-${HOME}/.local/share/ai-agent/copilot}"
VENV_DIR="${INSTALL_DIR}/.venv"
MCP_SERVER="${INSTALL_DIR}/mcp/copilot_mcp_server.py"
WRAPPER="${INSTALL_DIR}/bin/copilot_wrapper.sh"
CONFIG_FILE="${INSTALL_DIR}/config.yaml"
LOG_DIR="${INSTALL_DIR}/logs"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=11
CLAUDE_CMD_TIMEOUT="${CLAUDE_CMD_TIMEOUT:-15}"

# -- Colors (disabled when stdout is not a terminal) --
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' BOLD='' NC=''
fi

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# Default: skip automatic Claude Code registration to avoid hangs during install.
# To enable automatic registration set SKIP_CLAUDE_REGISTRATION=0 or SKIP_CLAUDE_REGISTRATION=false
# (advanced users). The installer still prints the manual `claude mcp add ...` command.
if [[ -z "${SKIP_CLAUDE_REGISTRATION+x}" ]]; then
    SKIP_CLAUDE_REGISTRATION=1
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
need_sudo() {
    local dir="$1"
    if [[ -d "$dir" ]]; then
        [[ ! -w "$dir" ]]
    else
        local parent="$dir"
        while [[ ! -d "$parent" ]]; do
            parent="$(dirname "$parent")"
        done
        [[ ! -w "$parent" ]]
    fi
}

run_privileged() {
    if [[ "$(id -u)" -eq 0 ]]; then
        "$@"
    elif command -v sudo &>/dev/null; then
        sudo "$@"
    else
        die "Root privileges required but sudo is not available. Run as root."
    fi
}

# Portable timeout: GNU timeout → Homebrew gtimeout → Python subprocess
# (Python 3.11+ is guaranteed available after setup_venv; for early calls
# before Python is confirmed, falls back to unbounded execution as last resort).
_timeout_cmd() {
    local secs="$1"; shift
    if command -v timeout &>/dev/null; then
        # Run command in foreground and disallow interactive prompts by redirecting stdin
        timeout --foreground "$secs" "$@" < /dev/null
        return $?
    elif command -v gtimeout &>/dev/null; then
        gtimeout --foreground "$secs" "$@" < /dev/null
        return $?
    fi
    local py_bin="${PYTHON_BIN:-$(command -v python3 2>/dev/null || true)}"
    if [[ -n "$py_bin" && -x "$py_bin" ]]; then
        TIMEOUT_SECS="$secs" "$py_bin" - "$@" <<'PY'
import subprocess, sys, os
try:
    r = subprocess.run(sys.argv[1:], timeout=int(os.environ["TIMEOUT_SECS"]), stdin=subprocess.DEVNULL)
    sys.exit(r.returncode)
except subprocess.TimeoutExpired:
    sys.exit(124)
PY
        return $?
    fi
    # No timeout implementation available — run unbounded (last resort)
    "$@" < /dev/null
}

# ---------------------------------------------------------------------------
# INSTALL_DIR state file — persist and reload the install path so that bare
# `--status` / `--update` work after a non-default or system-wide install.
# ---------------------------------------------------------------------------
_state_file_path() {
    # Always in the real user's home (SUDO_USER when under sudo).
    local user_home="$HOME"
    if [[ "$(id -u)" -eq 0 && -n "${SUDO_USER:-}" ]]; then
        local sh
        sh="$(resolve_user_home "$SUDO_USER" 2>/dev/null || true)"
        [[ -n "$sh" ]] && user_home="$sh"
    fi
    echo "${user_home}/.local/share/ai-agent/copilot-install-dir"
}

save_install_dir() {
    local state_file state_dir
    state_file="$(_state_file_path)"
    state_dir="$(dirname "$state_file")"
    mkdir -p "$state_dir" 2>/dev/null || true
    printf '%s\n' "${INSTALL_DIR}" > "$state_file" || true
    # Fix ownership so the real user can read it after a sudo install
    if [[ "$(id -u)" -eq 0 && -n "${SUDO_USER:-}" ]]; then
        local uid gid
        uid="$(id -u "$SUDO_USER" 2>/dev/null || echo 0)"
        gid="$(id -g "$SUDO_USER" 2>/dev/null || echo 0)"
        chown "${uid}:${gid}" "$state_file" "$state_dir" 2>/dev/null || true
    fi
}

load_install_dir() {
    # Skip when INSTALL_DIR was explicitly provided via environment
    [[ -z "${INSTALL_DIR_EXPLICIT:-}" ]] || return 0
    local state_file
    state_file="$(_state_file_path)"
    [[ -f "$state_file" ]] || return 0
    local saved
    # IFS= read -r preserves internal spaces; strips only the trailing newline.
    IFS= read -r saved < "$state_file" 2>/dev/null || saved=""
    [[ -n "$saved" && -d "$saved" ]] || return 0
    INSTALL_DIR="$saved"
    VENV_DIR="${INSTALL_DIR}/.venv"
    MCP_SERVER="${INSTALL_DIR}/mcp/copilot_mcp_server.py"
    WRAPPER="${INSTALL_DIR}/bin/copilot_wrapper.sh"
    CONFIG_FILE="${INSTALL_DIR}/config.yaml"
    LOG_DIR="${INSTALL_DIR}/logs"
}

# Recursively re-own INSTALL_DIR to SUDO_USER after all steps have written
# their files as root.  No-op when not running under sudo.
fix_ownership() {
    [[ "$(id -u)" -eq 0 && -n "${SUDO_USER:-}" ]] || return 0
    local uid gid
    uid="$(id -u "$SUDO_USER" 2>/dev/null || id -u)"
    gid="$(id -g "$SUDO_USER" 2>/dev/null || id -g)"
    run_privileged chown -R "${uid}:${gid}" "${INSTALL_DIR}"
    info "Ownership of ${INSTALL_DIR} set to ${SUDO_USER} (${uid}:${gid})"
}

resolve_user_home() {
    # All branches now check for non-empty output before returning 0.
    # Previously "return 0" was unconditional, so a non-existent username would
    # produce empty output but still indicate success, causing _check_not_sudo
    # and _state_file_path to fail-open on minimal containers.
    local username="$1"
    [[ -n "$username" && "$username" =~ ^[A-Za-z0-9._-]+$ ]] || return 1

    local result
    if command -v getent &>/dev/null; then
        result="$(getent passwd "$username" 2>/dev/null | cut -d: -f6)"
        [[ -n "$result" ]] && { printf '%s\n' "$result"; return 0; }
        return 1
    fi

    if command -v dscl &>/dev/null; then
        result="$(dscl . -read "/Users/${username}" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
        [[ -n "$result" ]] && { printf '%s\n' "$result"; return 0; }
        return 1
    fi

    # Fallback: parse /etc/passwd directly — available on all POSIX systems,
    # including minimal containers (Alpine, scratch-based) that lack getent/dscl.
    if [[ -r /etc/passwd ]]; then
        result="$(awk -F: -v u="$username" '$1 == u { print $6; exit }' /etc/passwd 2>/dev/null)"
        [[ -n "$result" ]] && { printf '%s\n' "$result"; return 0; }
        return 1
    fi

    return 1
}

find_copilot_cli_path() {
    local copilot_bin="${COPILOT_BIN:-}"

    if [[ -n "$copilot_bin" ]]; then
        if [[ -x "$copilot_bin" ]]; then
            printf '%s\n' "$copilot_bin"
            return 0
        fi
        local resolved
        resolved="$(command -v "$copilot_bin" 2>/dev/null || true)"
        if [[ -n "$resolved" && -x "$resolved" ]]; then
            printf '%s\n' "$resolved"
            return 0
        fi
        return 1
    fi

    local candidates=(
        "${HOME}/.local/bin/copilot"
        "/usr/local/bin/copilot"
        "/opt/homebrew/bin/copilot"
        "${HOME}/.npm-global/bin/copilot"        "${HOME}/node_modules/.bin/copilot"
        "${HOME}/.asdf/shims/copilot"
        "${HOME}/.local/share/mise/shims/copilot"
        "${HOME}/.local/share/rtx/shims/copilot"
    )

    local nvm_versions_dir="${HOME}/.nvm/versions/node"
    if [[ -d "$nvm_versions_dir" ]]; then
        local latest_node=""
        local py_sorter="${PYTHON_BIN:-$(command -v python3 2>/dev/null || true)}"
        if [[ -n "$py_sorter" && -x "$py_sorter" ]]; then
            latest_node="$(ls -1 "$nvm_versions_dir" 2>/dev/null \
                | "$py_sorter" -c "
import sys, re
def _ver(v):
    m = re.match(r'v?(\\d+)\\.(\\d+)\\.(\\d+)', v.strip())
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
versions = [l.strip() for l in sys.stdin if l.strip()]
print(max(versions, key=_ver) if versions else '', end='')
" 2>/dev/null || true)"
        fi
        if [[ -n "$latest_node" ]]; then
            candidates+=("${nvm_versions_dir}/${latest_node}/bin/copilot")
        else
            local nvm_candidate
            for nvm_candidate in "${nvm_versions_dir}"/*/bin/copilot; do
                [[ -e "$nvm_candidate" ]] || continue
                candidates+=("$nvm_candidate")
            done
        fi
    fi

    local candidate
    for candidate in "${candidates[@]}"; do
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    copilot_bin="$(command -v copilot 2>/dev/null || true)"
    if [[ -n "$copilot_bin" && -x "$copilot_bin" ]]; then
        printf '%s\n' "$copilot_bin"
        return 0
    fi

    return 1
}

# ---------------------------------------------------------------------------
# 1. OS detection
# ---------------------------------------------------------------------------
detect_os() {
    UNAME="$(uname -s)"
    case "$UNAME" in
        Linux)
            if [[ -f /etc/os-release ]]; then
                . /etc/os-release
                OS_ID="${ID:-linux}"
                OS_LIKE="${ID_LIKE:-$OS_ID}"
            else
                OS_ID="linux"
                OS_LIKE="linux"
            fi
            ;;
        Darwin)
            OS_ID="macos"
            OS_LIKE="macos"
            ;;
        MINGW*|MSYS*|CYGWIN*)
            OS_ID="windows"
            OS_LIKE="windows"
            ;;
        *)
            OS_ID="unknown"
            OS_LIKE="unknown"
            ;;
    esac
    info "OS: ${OS_ID} ($(uname -m))"
}

# ---------------------------------------------------------------------------
# 2. Python check / install
# ---------------------------------------------------------------------------
check_python_version() {
    local py="$1"
    local full_path
    full_path="$(command -v "$py" 2>/dev/null)" || return 1
    local ver major minor
    ver=$("$full_path" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null) || return 1
    major="${ver%%.*}"
    minor="${ver#*.}"
    if (( major > MIN_PYTHON_MAJOR )) || (( major == MIN_PYTHON_MAJOR && minor >= MIN_PYTHON_MINOR )); then
        echo "$full_path"
        return 0
    fi
    return 1
}

find_python() {
    for candidate in python3.13 python3.12 python3.11 python3; do
        local result
        if result=$(check_python_version "$candidate"); then
            PYTHON_BIN="$result"
            info "Python: $("$PYTHON_BIN" --version) ($PYTHON_BIN)"
            return 0
        fi
    done
    return 1
}

install_python() {
    info "Python >= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} not found. Attempting to install..."
    local pyver
    local pkg_installed=0

    case "$OS_LIKE" in
        *debian*|*ubuntu*)
            run_privileged apt-get update -qq
            for pyver in 3.13 3.12 3.11; do
                if run_privileged apt-get install -y "python${pyver}" "python${pyver}-venv"; then
                    pkg_installed=1; break
                fi
            done
            ;;
        *fedora*)
            for pyver in 3.13 3.12 3.11; do
                if run_privileged dnf install -y "python${pyver}"; then
                    pkg_installed=1; break
                fi
            done
            ;;
        *rhel*|*centos*|*rocky*|*alma*)
            if command -v dnf &>/dev/null; then
                for pyver in 3.13 3.12 3.11; do
                    if run_privileged dnf install -y "python${pyver}"; then
                        pkg_installed=1; break
                    fi
                done
            elif command -v yum &>/dev/null; then
                for pyver in 3.13 3.12 3.11; do
                    if run_privileged yum install -y "python${pyver}"; then
                        pkg_installed=1; break
                    fi
                done
            fi
            [[ "$pkg_installed" -eq 1 ]] || die "Could not install Python 3.11+ on ${OS_ID}. Install manually."
            ;;
        *arch*|*manjaro*)
            run_privileged pacman -Sy --noconfirm python python-pip \
                || die "Could not install Python on ${OS_ID}. Install manually."
            pkg_installed=1
            ;;
        *suse*|*opensuse*)
            run_privileged zypper install -y python3 python3-pip python3-venv \
                || die "Could not install Python on ${OS_ID}. Install manually."
            pkg_installed=1
            ;;
        macos)
            if command -v brew &>/dev/null; then
                brew update || warn "brew update failed, continuing with cached formulae..."
                # If all three formulas fail the last command exits non-zero.
                # Under set -e the script would die silently — add an explicit die.
                brew install python@3.13 || \
                brew install python@3.12 || \
                brew install python@3.11 || \
                    die "Could not install Python 3.11+ via Homebrew. Install manually: brew install python@3.11"
                pkg_installed=1
            else
                die "Python 3.11+ not found and Homebrew is unavailable. Install Python manually: https://www.python.org/downloads/"
            fi
            ;;
        *)
            die "Unsupported OS (${OS_ID}). Install Python >= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} manually."
            ;;
    esac

    if [[ "$pkg_installed" -eq 0 && "$OS_LIKE" =~ (debian|ubuntu|fedora) ]]; then
        die "Could not install Python 3.11+ on ${OS_ID}. Install manually."
    fi

    find_python || die "Python installation failed. Install Python >= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} manually."
}

# ---------------------------------------------------------------------------
# 3. Directory structure
# ---------------------------------------------------------------------------
setup_directories() {
    info "Install directory: ${INSTALL_DIR}"

    # Warn when running as root with a home-directory target: files would be
    # owned by root, preventing the unprivileged Claude Code process from
    # executing the MCP server.
    if [[ "$(id -u)" -eq 0 && "${INSTALL_DIR}" == "${HOME}"* ]]; then
        warn "Running as root but INSTALL_DIR (${INSTALL_DIR}) is inside \$HOME."
        warn "Installed files will be owned by root, which may prevent Claude Code"
        warn "from starting the MCP server as a non-root user."
        warn "Consider running without sudo or set INSTALL_DIR to a system path."
    fi

    if need_sudo "${INSTALL_DIR}"; then
        run_privileged mkdir -p "${INSTALL_DIR}/bin" "${INSTALL_DIR}/mcp" "${LOG_DIR}"
        # chown to the real user (SUDO_USER when under sudo) so they can write
        # logs and edit config without elevated privileges after installation.
        local owner_uid owner_gid
        if [[ "$(id -u)" -eq 0 && -n "${SUDO_USER:-}" ]]; then
            owner_uid="$(id -u "$SUDO_USER" 2>/dev/null || id -u)"
            owner_gid="$(id -g "$SUDO_USER" 2>/dev/null || id -g)"
        else
            owner_uid="$(id -u)"
            owner_gid="$(id -g)"
        fi
        run_privileged chown -R "${owner_uid}:${owner_gid}" "${INSTALL_DIR}"
    else
        mkdir -p "${INSTALL_DIR}/bin" "${INSTALL_DIR}/mcp" "${LOG_DIR}"
    fi
}

# ---------------------------------------------------------------------------
# 4. Virtualenv and dependencies
# ---------------------------------------------------------------------------
setup_venv() {
    # Warn when the venv will be owned by a different user than the one who
    # will run Claude Code.  This happens when the script runs as root (via
    # sudo) but $HOME was not updated to the root home (i.e. without sudo -H).
    # In that case VENV_DIR may be under the real user's home, yet files will
    # be owned by root — causing "Permission denied" at MCP server startup.
    if [[ "$(id -u)" -eq 0 && -n "${SUDO_USER:-}" ]]; then
        local sudo_home
        sudo_home="$(resolve_user_home "$SUDO_USER" || true)"
        if [[ -n "$sudo_home" && "${VENV_DIR}" == "${sudo_home}"* ]]; then
            warn "Running as root (sudo) but VENV_DIR (${VENV_DIR}) is inside"
            warn "${SUDO_USER}'s home.  Without 'sudo -H', the virtualenv will be"
            warn "owned by root and ${SUDO_USER} cannot start the MCP server."
            warn "Fix: run 'sudo chown -R ${SUDO_USER}: ${VENV_DIR}' after install,"
            warn "or re-run as '${SUDO_USER}' without sudo."
        fi
    fi

    if [[ -d "${VENV_DIR}" && -f "${VENV_DIR}/bin/python" ]]; then
        info "Existing venv found, updating dependencies..."
    else
        info "Creating Python virtualenv..."
        "$PYTHON_BIN" -m venv "${VENV_DIR}"
    fi

    if [[ ! -x "${VENV_DIR}/bin/pip" ]]; then
        "${VENV_DIR}/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || \
        die "pip is unavailable in the virtualenv. Install a Python build with venv/ensurepip support."
    fi

    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    "${VENV_DIR}/bin/pip" install --quiet "mcp[cli]>=1.20" "pyyaml>=6.0" "trio>=0.27"
    info "Python dependencies installed."
}

# ---------------------------------------------------------------------------
# 5. Copilot CLI detection
# ---------------------------------------------------------------------------
detect_copilot_cli() {
    local copilot_bin=""
    copilot_bin="$(find_copilot_cli_path || true)"

    if [[ -n "$copilot_bin" ]]; then
        info "Copilot CLI: ${copilot_bin}"
    else
        warn "Copilot CLI not found."
        warn "Set it later: export COPILOT_BIN=/path/to/copilot"
        copilot_bin="copilot"
    fi

    COPILOT_CLI_PATH="$copilot_bin"
}

# ---------------------------------------------------------------------------
# 6. Deploy files from src/
# ---------------------------------------------------------------------------
deploy_files() {
    # Verify source files exist — guards against running the script from a
    # location where src/ is not present (e.g. a bare copy of the installer).
    [[ -f "${SCRIPT_DIR}/src/copilot_mcp_server.py" ]] || \
        die "src/copilot_mcp_server.py not found next to installer (SCRIPT_DIR=${SCRIPT_DIR})"
    [[ -f "${SCRIPT_DIR}/src/copilot_wrapper.sh" ]] || \
        die "src/copilot_wrapper.sh not found next to installer (SCRIPT_DIR=${SCRIPT_DIR})"
    [[ -f "${SCRIPT_DIR}/src/redact.py" ]] || \
        die "src/redact.py not found next to installer (SCRIPT_DIR=${SCRIPT_DIR})"
    [[ -f "${SCRIPT_DIR}/config.yaml" ]] || \
        die "config.yaml not found next to installer (SCRIPT_DIR=${SCRIPT_DIR})"

    info "Deploying MCP server..."
    cp "${SCRIPT_DIR}/src/copilot_mcp_server.py" "${INSTALL_DIR}/mcp/copilot_mcp_server.py"
    chmod +x "${INSTALL_DIR}/mcp/copilot_mcp_server.py"

    info "Deploying wrapper..."
    cp "${SCRIPT_DIR}/src/copilot_wrapper.sh" "${INSTALL_DIR}/bin/copilot_wrapper.sh"
    chmod +x "${INSTALL_DIR}/bin/copilot_wrapper.sh"

    # redact.py is the single source of truth for secret-redaction patterns.
    # It must be present in both bin/ (for the wrapper) and mcp/ (for the server).
    info "Deploying redact.py..."
    cp "${SCRIPT_DIR}/src/redact.py" "${INSTALL_DIR}/bin/redact.py"
    cp "${SCRIPT_DIR}/src/redact.py" "${INSTALL_DIR}/mcp/redact.py"

    # Always update the reference copy so users can diff new profiles/options
    cp "${SCRIPT_DIR}/config.yaml" "${CONFIG_FILE}.default"

    # Deploy config only if not already present (preserve user customizations)
    if [[ ! -f "${CONFIG_FILE}" ]]; then
        info "Deploying default config..."
        cp "${SCRIPT_DIR}/config.yaml" "${CONFIG_FILE}"
    else
        info "Config already exists, skipping (${CONFIG_FILE})"
        info "New options available in: ${CONFIG_FILE}.default"
    fi
}

# ---------------------------------------------------------------------------
# 7. Register MCP server with Claude Code
# ---------------------------------------------------------------------------
sq_escape() {
    # Make a string safe to embed inside single quotes by replacing each ' with '\''
    # (end-quote, literal apostrophe, re-open-quote).
    # Example: o'connor → o'\''connor  →  wrapped: 'o'\''connor'  →  evals to: o'connor
    printf '%s' "${1//\'/\'\\\'\'}"
}

print_manual_claude_registration() {
    # Single-quote each path so spaces and shell-special chars (including apostrophes)
    # in INSTALL_DIR / COPILOT_CLI_PATH don't break the printed command.
    local q_install q_bin q_python q_server
    q_install="'$(sq_escape "${INSTALL_DIR}")'"
    q_bin="'$(sq_escape "${COPILOT_CLI_PATH}")'"
    q_python="'$(sq_escape "${VENV_DIR}/bin/python")'"
    q_server="'$(sq_escape "${MCP_SERVER}")'"
    echo "  claude mcp add -s user copilot-delegate \\"
    echo "    -e COPILOT_INSTALL_DIR=${q_install} \\"
    echo "    -e COPILOT_BIN=${q_bin} \\"
    echo "    -- ${q_python} ${q_server}"
}

capture_existing_claude_registration() {
    CLAUDE_CAPTURE_TIMEOUT="${CLAUDE_CMD_TIMEOUT}" "$PYTHON_BIN" - <<'PY'
import json
import shlex
import subprocess
import sys
import os

try:
    proc = subprocess.run(
        ["claude", "mcp", "get", "copilot-delegate"],
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("CLAUDE_CAPTURE_TIMEOUT", "15")),
        check=False,
    )
    text = (proc.stdout or "") + (proc.stderr or "")
except subprocess.TimeoutExpired as exc:
    stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    text = stdout + stderr

data = {"scope": "", "command": "", "args": [], "env": {}}
in_env = False

for raw_line in text.splitlines():
    line = raw_line.strip()
    if not line:
        continue
    if line.startswith("Scope: "):
        scope_line = line.split(": ", 1)[1].lower()
        if scope_line.startswith("user"):
            data["scope"] = "user"
        elif scope_line.startswith("project"):
            data["scope"] = "project"
        elif scope_line.startswith("local"):
            data["scope"] = "local"
    elif line.startswith("Command: "):
        data["command"] = line.split(": ", 1)[1]
    elif line.startswith("Args: "):
        args_line = line.split(": ", 1)[1].strip()
        data["args"] = shlex.split(args_line) if args_line else []
    elif line.startswith("Environment:"):
        in_env = True
    elif in_env:
        if line.startswith("To remove this server"):
            break
        if "=" not in line:
            in_env = False
            continue
        key, value = line.split("=", 1)
        data["env"][key] = value

if not data["command"]:
    sys.exit(1)

print(json.dumps(data, separators=(",", ":")))
PY
}

run_claude_mcp_cmd() {
    _timeout_cmd "${CLAUDE_CMD_TIMEOUT}" claude "$@"
}

restore_claude_registration() {
    local backup_json="$1"
    BACKUP_JSON="$backup_json" CLAUDE_RESTORE_TIMEOUT="${CLAUDE_CMD_TIMEOUT}" "$PYTHON_BIN" - <<'PY'
import json
import os
import subprocess
import sys

cfg = json.loads(os.environ["BACKUP_JSON"])
cmd = ["claude", "mcp", "add"]
if cfg.get("scope"):
    cmd.extend(["-s", cfg["scope"]])
cmd.append("copilot-delegate")
for key, value in sorted(cfg.get("env", {}).items()):
    cmd.extend(["-e", f"{key}={value}"])
cmd.extend(["--", cfg["command"]])
cmd.extend(cfg.get("args", []))
try:
    sys.exit(
        subprocess.run(
            cmd,
            check=False,
            timeout=int(os.environ.get("CLAUDE_RESTORE_TIMEOUT", "15")),
        ).returncode
    )
except subprocess.TimeoutExpired:
    sys.exit(124)
PY
}

extract_registration_scope() {
    local backup_json="$1"
    BACKUP_JSON="$backup_json" "$PYTHON_BIN" - <<'PY'
import json
import os
print(json.loads(os.environ["BACKUP_JSON"]).get("scope", ""), end="")
PY
}

register_claude_mcp() {
    info "Registering with Claude Code..."

    # Allow skipping automatic Claude Code registration for non-interactive installs
    # Set SKIP_CLAUDE_REGISTRATION=1 or SKIP_CLAUDE_REGISTRATION=true in the environment to skip.
    if [[ "${SKIP_CLAUDE_REGISTRATION:-}" = "1" || "${SKIP_CLAUDE_REGISTRATION:-}" = "true" ]]; then
        warn "SKIP_CLAUDE_REGISTRATION set — skipping automatic Claude Code registration."
        echo ""
        print_manual_claude_registration
        echo ""
        return 0
    fi

    if [[ "$(id -u)" -eq 0 && -n "${SUDO_USER:-}" ]]; then
        warn "System-wide install detected under sudo; skipping automatic Claude Code registration."
        warn "Run this as '${SUDO_USER}' to register the MCP server in that user's Claude config:"
        echo ""
        print_manual_claude_registration
        echo ""
        return 0
    fi

    if ! command -v claude &>/dev/null; then
        warn "Claude Code CLI not found in PATH."
        echo ""
        warn "After installing Claude Code, run manually:"
        print_manual_claude_registration
        echo ""
        return 0
    fi

    # Quick non-interactive health check: if the claude CLI requires interactive
    # login or otherwise blocks, skip automatic registration to avoid hanging the
    # installer. Use a short timeout so the installer stays responsive.
    if ! _timeout_cmd 3 claude --version >/dev/null 2>&1; then
        warn "Claude CLI appears to be interactive or unresponsive; skipping automatic Claude Code registration."
        echo ""
        print_manual_claude_registration
        echo ""
        return 0
    fi

    local had_existing=0
    local backup_json=""
    local existing_scope=""
    local add_rc=0
    local remove_rc=0

    # Avoid `claude mcp list`: it performs health checks for every configured MCP
    # server and can be unexpectedly slow or appear hung when unrelated servers are
    # unhealthy. `claude mcp get copilot-delegate` scopes the work to this server.
    backup_json="$(capture_existing_claude_registration || true)"
    if [[ -n "$backup_json" ]]; then
        had_existing=1
        existing_scope="$(extract_registration_scope "$backup_json")"
        if [[ -n "$existing_scope" ]]; then
            run_claude_mcp_cmd mcp remove -s "$existing_scope" copilot-delegate >/dev/null 2>&1 || remove_rc=$?
        else
            run_claude_mcp_cmd mcp remove copilot-delegate >/dev/null 2>&1 || remove_rc=$?
        fi
        if [[ "$remove_rc" -ne 0 ]]; then
            if [[ "$remove_rc" -eq 124 ]]; then
                die "Timed out while removing the existing Claude MCP registration. Remove it manually and rerun the installer."
            fi
            die "Failed to remove existing MCP registration from Claude Code."
        fi
    fi

    local -a add_args=()
    if [[ -n "$existing_scope" ]]; then
        add_args+=(-s "$existing_scope")
    else
        add_args+=(-s user)
    fi
    add_args+=(copilot-delegate \
        -e "COPILOT_INSTALL_DIR=${INSTALL_DIR}" \
        -e "COPILOT_BIN=${COPILOT_CLI_PATH}" \
        -- "${VENV_DIR}/bin/python" "${MCP_SERVER}")

    # Run registration in a backgrounded subshell so the installer remains
    # responsive and the user can interrupt (Ctrl+C). Capture output to a
    # temporary log file for debugging on failure.
    local reg_log reg_child prev_trap_int prev_trap_term
    reg_log="$(mktemp -t copilot_reg.XXXXXX 2>/dev/null || mktemp)"
    reg_child=0

    # Preserve existing traps so they can be restored after the registration
    prev_trap_int="$(trap -p INT || true)"
    prev_trap_term="$(trap -p TERM || true)"

    (
        run_claude_mcp_cmd mcp add "${add_args[@]}"
    ) >"${reg_log}" 2>&1 &
    reg_child=$!

    # If the user interrupts, kill the registration child and exit promptly
    trap 'if [[ -n "${reg_child:-}" && "${reg_child}" -ne 0 ]]; then kill "${reg_child}" 2>/dev/null || true; fi; echo ""; warn "Registration interrupted by user."; exit 130' INT TERM

    wait "$reg_child" || add_rc=$?

    # Restore original traps
    if [[ -n "${prev_trap_int}" ]]; then
        eval "${prev_trap_int}" || true
    else
        trap - INT
    fi
    if [[ -n "${prev_trap_term}" ]]; then
        eval "${prev_trap_term}" || true
    else
        trap - TERM
    fi

    if [[ "$add_rc" -ne 0 ]]; then
        if [[ "$had_existing" -eq 1 ]]; then
            warn "New MCP registration failed; attempting to restore the previous Claude configuration."
            restore_claude_registration "$backup_json" >/dev/null 2>&1 || true
        fi
        if [[ "$add_rc" -eq 124 ]]; then
            warn "Timed out while registering the MCP server with Claude Code. Run the printed manual command or retry later."
        else
            warn "Failed to register MCP server with Claude Code (exit code ${add_rc})."
        fi
        echo ""
        print_manual_claude_registration
        echo ""
        warn "Registration log (first 200 lines):"
        if [[ -f "${reg_log}" ]]; then
            sed -n '1,200p' "${reg_log}" | sed 's/^/    /' || true
        fi
        rm -f "${reg_log}"
        warn "Continuing installation without automatic registration."
    else
        info "MCP server 'copilot-delegate' registered in Claude Code."
        rm -f "${reg_log}" || true
    fi
}

# ---------------------------------------------------------------------------
# 8. Verification
# ---------------------------------------------------------------------------
verify_installation() {
    info "Verifying installation..."
    local errors=0

    [[ -x "${WRAPPER}" ]]     || { warn "Wrapper missing: ${WRAPPER}"; ((errors++)) || true; }
    [[ -f "${MCP_SERVER}" ]]  || { warn "MCP server missing: ${MCP_SERVER}"; ((errors++)) || true; }
    [[ -f "${CONFIG_FILE}" ]] || { warn "Config missing: ${CONFIG_FILE}"; ((errors++)) || true; }

    if ! "${VENV_DIR}/bin/python" -c "from mcp.server.fastmcp import FastMCP; import yaml" 2>/dev/null; then
        warn "MCP SDK or PyYAML not working in venv"
        ((errors++)) || true
    fi

    if [[ "$errors" -eq 0 ]]; then
        info "All checks passed!"
    else
        warn "${errors} issue(s) found, see warnings above."
    fi
}

# ---------------------------------------------------------------------------
# --status: show installation status
# ---------------------------------------------------------------------------
show_status() {
    load_install_dir
    echo ""
    echo -e "${BOLD}===========================================${NC}"
    echo -e "${BOLD}  AI Agent Copilot - Status${NC}"
    echo -e "${BOLD}===========================================${NC}"
    echo ""

    local ok="${GREEN}OK${NC}"
    local missing="${RED}MISSING${NC}"

    echo -e "  Install dir:  ${INSTALL_DIR}"
    echo -e "  MCP server:   $([[ -f "$MCP_SERVER" ]] && echo -e "$ok" || echo -e "$missing")  ${MCP_SERVER}"
    echo -e "  Wrapper:      $([[ -x "$WRAPPER" ]] && echo -e "$ok" || echo -e "$missing")  ${WRAPPER}"
    echo -e "  Config:       $([[ -f "$CONFIG_FILE" ]] && echo -e "$ok" || echo -e "$missing")  ${CONFIG_FILE}"
    echo -e "  Venv python:  $([[ -x "${VENV_DIR}/bin/python" ]] && echo -e "$ok" || echo -e "$missing")"
    if [[ -f "${SCRIPT_DIR}/src/copilot_mcp_server.py" && -f "${MCP_SERVER}" ]]; then
        if cmp -s "${SCRIPT_DIR}/src/copilot_mcp_server.py" "${MCP_SERVER}"; then
            echo -e "  Repo sync:    ${ok}"
        else
            echo -e "  Repo sync:    ${YELLOW}UPDATE NEEDED${NC}  installed MCP server differs from checkout"
        fi
    fi

    if command -v claude &>/dev/null; then
        local mcp_status mcp_rc
        mcp_rc=0
        mcp_status="$(_timeout_cmd 3 claude mcp get copilot-delegate 2>&1)" || mcp_rc=$?
        if [[ -z "$mcp_status" ]] \
          || printf '%s' "$mcp_status" | grep -qi "no mcp server found\|not found"; then
            echo -e "  MCP registered: ${YELLOW}NOT FOUND${NC}"
        elif printf '%s' "$mcp_status" | grep -qi "failed\|error\|✗"; then
            echo -e "  MCP registered: ${RED}REGISTERED BUT NOT CONNECTING${NC}"
            # grep may find nothing (rc=1) if the output format differs — || true prevents set -e exit
            printf '%s\n' "$mcp_status" | grep -i "status\|error\|failed" | sed 's/^/    /' || true
        elif [[ $mcp_rc -eq 124 ]]; then
            # timeout fired but did not yield any actionable status text.
            echo -e "  MCP registered: ${YELLOW}TIMEOUT (claude mcp get did not respond in 3s)${NC}"
        else
            echo -e "  MCP registered: ${ok}"
        fi
    else
        echo -e "  MCP registered: ${YELLOW}claude CLI not in PATH${NC}"
    fi

    local copilot_bin=""
    copilot_bin="$(find_copilot_cli_path || true)"
    if [[ -n "$copilot_bin" ]]; then
        echo -e "  Copilot CLI:  ${ok}  ${copilot_bin}"
    else
        echo -e "  Copilot CLI:  ${YELLOW}NOT FOUND${NC}"
    fi

    echo ""
    echo "  Recent logs:"
    if [[ -f "${LOG_DIR}/copilot-mcp.log" ]]; then
        # Suppress errors: unreadable log (chmod 000) must not crash --status
        tail -5 "${LOG_DIR}/copilot-mcp.log" 2>/dev/null | sed 's/^/    /' \
            || echo "    (log file exists but cannot be read — check permissions)"
    else
        echo "    (no logs yet)"
    fi
    echo ""
}

# ---------------------------------------------------------------------------
# --update: redeploy files without reinstalling Python/venv
# ---------------------------------------------------------------------------
do_update() {
    load_install_dir
    echo ""
    echo -e "${BOLD}===========================================${NC}"
    echo -e "${BOLD}  AI Agent Copilot - Update${NC}"
    echo -e "${BOLD}===========================================${NC}"
    echo ""

    if [[ ! -d "${INSTALL_DIR}" ]]; then
        die "Not installed yet. Run without --update first."
    fi

    detect_os
    find_python || install_python
    setup_venv
    detect_copilot_cli
    deploy_files
    register_claude_mcp
    fix_ownership
    save_install_dir
    verify_installation

    echo ""
    info "Update complete! Restart Claude Code to pick up changes."
    echo ""
}

# ---------------------------------------------------------------------------
# Full install
# ---------------------------------------------------------------------------
do_install() {
    echo ""
    echo -e "${BOLD}===========================================${NC}"
    echo -e "${BOLD}  AI Agent Copilot - Installer${NC}"
    echo -e "${BOLD}===========================================${NC}"
    echo ""

    detect_os

    if find_python; then
        :
    else
        install_python
    fi

    setup_directories
    setup_venv
    detect_copilot_cli
    deploy_files
    register_claude_mcp
    fix_ownership
    save_install_dir
    verify_installation

    echo ""
    echo -e "${BOLD}===========================================${NC}"
    echo -e "${GREEN}  Installation complete!${NC}"
    echo -e "${BOLD}===========================================${NC}"
    echo ""
    echo "  MCP server:  ${MCP_SERVER}"
    echo "  Wrapper:     ${WRAPPER}"
    echo "  Config:      ${CONFIG_FILE}"
    echo "  Venv:        ${VENV_DIR}"
    echo "  Logs:        ${LOG_DIR}"
    echo ""
    echo "  Tools in Claude Code:"
    echo "    run_agent_simple       — code, boilerplate, refactors"
    echo "    run_agent_security     — security analysis"
    echo "    run_agent_code_review  — code quality review"
    echo ""
    echo "  Update:    ./install-copilot-agent.sh --update"
    echo "  Status:    ./install-copilot-agent.sh --status"
    echo "  Uninstall: rm -rf ${INSTALL_DIR} && claude mcp remove copilot-delegate"
    echo ""
}

# ---------------------------------------------------------------------------
# Guard: refuse to run as sudo only when INSTALL_DIR targets a user home dir.
# System-path installs (e.g. INSTALL_DIR=/opt/...) are allowed.
# ---------------------------------------------------------------------------
_check_not_sudo() {
    [[ "$(id -u)" -eq 0 && -n "${SUDO_USER:-}" ]] || return 0

    local sudo_home
    sudo_home="$(resolve_user_home "$SUDO_USER" || true)"

    # Block only when INSTALL_DIR is inside the invoking user's home directory.
    # A system path like /opt/ai-agent/copilot is perfectly fine under sudo.
    if [[ -n "$sudo_home" && "${INSTALL_DIR}" == "${sudo_home}"* ]]; then
        echo ""
        echo -e "${RED}[ERROR]${NC} Do not run this installer with sudo when INSTALL_DIR"
        echo "        targets '${SUDO_USER}' home directory (${sudo_home})."
        echo "        Files owned by root cannot be used by '${SUDO_USER}'."
        echo ""
        echo "        Run without sudo:"
        echo "          ./install-copilot-agent.sh ${*}"
        echo ""
        echo "        For a system-wide install, set INSTALL_DIR to a system path:"
        echo "          sudo INSTALL_DIR=/opt/ai-agent/copilot ./install-copilot-agent.sh ${*}"
        echo ""
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if [[ "${INSTALLER_LIB_ONLY:-0}" != "1" ]]; then
    case "${1:-}" in
        --update) _check_not_sudo --update; do_update ;;
        --status) show_status ;;
        --register-now) SKIP_CLAUDE_REGISTRATION=0; _check_not_sudo; do_install ;;
        "")       _check_not_sudo; do_install ;;
        *)
            echo ""
            echo -e "${RED}[ERROR]${NC} Unknown argument: '${1}'"
            echo ""
            echo "Usage: $(basename "${BASH_SOURCE[0]}") [--update | --status | --register-now]"
            echo ""
            echo "  (no arguments)  Full installation (defaults to skipping Claude registration)"
            echo "  --register-now   Full installation and attempt automatic Claude registration (may block if claude CLI is interactive)"
            echo "  --update        Update files only, skip Python/venv setup"
            echo "  --status        Show installation status"
            echo ""
            exit 1
            ;;
    esac
fi
