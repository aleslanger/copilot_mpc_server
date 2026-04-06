#!/usr/bin/env bash
# Wrapper that invokes the Copilot CLI with safety checks.
#
# Usage:
#   copilot_wrapper.sh [OPTIONS] [--] TASK
#
# Options:
#   --model MODEL               Copilot model (sets COPILOT_MODEL env var)
#   --prompt-prefix PREFIX      Text prepended to task before sending to Copilot
#   --max-input N               Maximum task length in characters (default 5000)
#   --max-output N              Maximum output length in characters (default 16000)
#   --timeout N                 Copilot execution timeout in seconds (default 300)
#   --blocked-pattern PATTERN   Reject task if it matches PATTERN (repeatable)
#   --allowed-tool TOOL         Whitelist TOOL for Copilot (repeatable); absent = all blocked
#
# Exit codes (documented here as the single authoritative reference):
#   0   Success
#   1   Missing or empty task argument / unknown option
#   2   Task exceeds --max-input length
#   3   Task contains disallowed control characters
#   4   Task matches a --blocked-pattern
#   5   Copilot CLI execution failed (non-zero exit or timeout)
#   6   Copilot CLI binary not found or not executable
#   7   Python interpreter not available (required for normalization / redaction / truncation)
set -euo pipefail

TIMEOUT_SECONDS=300
MAX_INPUT_LENGTH=5000
MAX_OUTPUT=16000
# ^ defaults, overridden by --timeout / --max-input / --max-output flags

COPILOT_BIN="${COPILOT_BIN:-copilot}"
VENV_PYTHON="${COPILOT_INSTALL_DIR:-${HOME}/.local/share/ai-agent/copilot}/.venv/bin/python"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
MODEL=""
PROMPT_PREFIX=""
# Blocked patterns and allowed tools are passed from Python (sourced from config.yaml)
# so that config.yaml is the single source of truth — no hardcoded values here.
BLOCK_PATTERNS=()
ALLOWED_TOOLS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            [[ -n "${2:-}" ]] || { echo "ERROR: --model requires a value" >&2; exit 1; }
            MODEL="$2"
            shift 2
            ;;
        --prompt-prefix)
            [[ -n "${2:-}" ]] || { echo "ERROR: --prompt-prefix requires a value" >&2; exit 1; }
            PROMPT_PREFIX="$2"
            shift 2
            ;;
        --max-input)
            [[ -n "${2:-}" ]] || { echo "ERROR: --max-input requires a value" >&2; exit 1; }
            [[ "${2}" =~ ^[1-9][0-9]*$ ]] || { echo "ERROR: --max-input must be a positive integer (> 0)" >&2; exit 1; }
            MAX_INPUT_LENGTH="$2"
            shift 2
            ;;
        --max-output)
            [[ -n "${2:-}" ]] || { echo "ERROR: --max-output requires a value" >&2; exit 1; }
            [[ "${2}" =~ ^[1-9][0-9]*$ ]] || { echo "ERROR: --max-output must be a positive integer (> 0)" >&2; exit 1; }
            MAX_OUTPUT="$2"
            shift 2
            ;;
        --timeout)
            [[ -n "${2:-}" ]] || { echo "ERROR: --timeout requires a value" >&2; exit 1; }
            [[ "${2}" =~ ^[1-9][0-9]*$ ]] || { echo "ERROR: --timeout must be a positive integer (> 0)" >&2; exit 1; }
            TIMEOUT_SECONDS="$2"
            shift 2
            ;;
        --blocked-pattern)
            [[ -n "${2:-}" ]] || { echo "ERROR: --blocked-pattern requires a value" >&2; exit 1; }
            BLOCK_PATTERNS+=("$2")
            shift 2
            ;;
        --allowed-tool)
            [[ -n "${2:-}" ]] || { echo "ERROR: --allowed-tool requires a value" >&2; exit 1; }
            # Reject tool names that contain commas — they would silently
            # expand into multiple entries in the --available-tools CSV, bypassing
            # the whitelist intent.  Caller must pass each tool as a separate flag.
            if printf '%s' "$2" | grep -qF ','; then
                echo "ERROR: --allowed-tool value '${2}' contains a comma; pass each tool separately" >&2
                exit 1
            fi
            ALLOWED_TOOLS+=("$2")
            shift 2
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "ERROR: Unknown option: $1" >&2
            exit 1
            ;;
        *)
            break
            ;;
    esac
done

if [[ $# -lt 1 ]]; then
    echo "ERROR: Missing task" >&2
    exit 1
fi

TASK="$1"

if [[ -z "$TASK" ]]; then
    echo "ERROR: Empty task" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Extend PATH before any binary lookups so locations added here are visible
# to command -v below. Preserves the original PATH so version managers
# (asdf, nvm, mise) that install Copilot in non-standard paths continue to work.
# ---------------------------------------------------------------------------
export PATH="${HOME}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:${PATH:-/usr/bin:/bin}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
pick_python_bin() {
    if [[ -x "$VENV_PYTHON" ]]; then
        printf '%s\n' "$VENV_PYTHON"
        return 0
    fi
    local python_bin
    python_bin="$(command -v python3 2>/dev/null || true)"
    if [[ -n "$python_bin" ]]; then
        printf '%s\n' "$python_bin"
        return 0
    fi
    return 1
}

contains_disallowed_control_chars() {
    # Pure-bash implementation — no subprocess overhead.
    # Strips the allowed whitespace control chars (\t, \n, \r) via parameter
    # expansion, then tests the remainder against POSIX [[:cntrl:]] which covers
    # ASCII 0x00–0x1F and 0x7F (DEL).
    #
    # Unicode safety: in a UTF-8 locale bash's ERE engine treats [[:cntrl:]] as
    # matching only the ASCII control range; UTF-8 continuation bytes (0x80–0xBF)
    # and leading bytes (0xC0–0xFF) fall outside this class and are not matched.
    # Null bytes (0x00) cannot be stored in bash variables and are therefore
    # implicitly excluded — this matches the original Python behaviour.
    local task="$1"
    local stripped="${task//$'\t'/}"
    stripped="${stripped//$'\n'/}"
    stripped="${stripped//$'\r'/}"
    [[ "$stripped" =~ [[:cntrl:]] ]]
}

run_with_timeout() {
    if command -v timeout >/dev/null 2>&1; then
        timeout "$TIMEOUT_SECONDS" "$@"
        return $?
    fi
    if command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$TIMEOUT_SECONDS" "$@"
        return $?
    fi
    local python_bin
    python_bin="$(pick_python_bin)" || {
        echo "ERROR: No timeout implementation available" >&2
        return 127
    }
    "$python_bin" - "$TIMEOUT_SECONDS" "$@" <<'PY_EOF'
import subprocess, sys, os, signal
timeout_seconds = int(sys.argv[1])
command = sys.argv[2:]
# Use Popen + process group so we can kill the whole child tree on timeout.
proc = subprocess.Popen(command, start_new_session=True)
try:
    proc.wait(timeout=timeout_seconds)
except subprocess.TimeoutExpired:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        pass
    sys.exit(124)
sys.exit(proc.returncode)
PY_EOF
}

ts() {
    date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "unknown-time"
}

make_temp_file() {
    local dir="$1"
    mkdir -p "$dir" 2>/dev/null || return 1
    # Use a full-path template instead of GNU-only `mktemp -p` so this works
    # on both GNU/Linux and BSD/macOS.
    mktemp "${dir%/}/copilot_XXXXXX" 2>/dev/null
}

create_temp_pair() {
    local dir="$1"
    local out err
    out="$(make_temp_file "$dir")" || return 1
    err="$(make_temp_file "$dir")" || {
        rm -f "$out"
        return 1
    }
    TMP_OUT="$out"
    TMP_ERR="$err"
    return 0
}

# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
if [[ ${#TASK} -gt $MAX_INPUT_LENGTH ]]; then
    echo "ERROR: Task too long" >&2
    exit 2
fi

if contains_disallowed_control_chars "$TASK"; then
    echo "ERROR: Control characters detected" >&2
    exit 3
fi

python_bin="$(pick_python_bin)" || {
    echo "ERROR: Python is required for task normalization, secret redaction, and Unicode-safe truncation" >&2
    exit 7
}

# Normalize (NFKC), strip zero-width, collapse whitespace, and lowercase to defeat obfuscation
NORMALIZED_LOWER="$(
    printf '%s' "$TASK" | "$python_bin" -c 'import sys,unicodedata,re
text = sys.stdin.read()
text = unicodedata.normalize("NFKC", text)
text = re.sub(r"[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]", "", text)
print(" ".join(text.split()).lower())'
)"

for pattern in "${BLOCK_PATTERNS[@]}"; do
    # Lowercase the pattern too so matching is case-insensitive on both sides.
    # Without this, a user-defined rule like "DROP TABLE" would never match
    # the already-lowercased task text.
    pattern_lower="$(printf '%s' "$pattern" | tr '[:upper:]' '[:lower:]')"
    if printf '%s' "$NORMALIZED_LOWER" | grep -qF -e "$pattern_lower"; then
        echo "ERROR: Dangerous pattern detected" >&2
        exit 4
    fi
done

# ---------------------------------------------------------------------------
# Verify Copilot CLI is available — done after input validation so that
# bad input always produces the documented exit codes (2/3/4) regardless of
# whether Copilot is installed on this machine.
# ---------------------------------------------------------------------------
_resolved_copilot="$(command -v "$COPILOT_BIN" 2>/dev/null || true)"
if [[ -z "$_resolved_copilot" || ! -x "$_resolved_copilot" ]]; then
    echo "ERROR: Copilot CLI not found or not executable: ${COPILOT_BIN}" >&2
    echo "       Set COPILOT_BIN to the full path of the copilot binary." >&2
    exit 6
fi
COPILOT_BIN="$_resolved_copilot"

# ---------------------------------------------------------------------------
# Build final prompt (prefix + task)
# ---------------------------------------------------------------------------
if [[ -n "$PROMPT_PREFIX" ]]; then
    FINAL_PROMPT="${PROMPT_PREFIX}

${TASK}"
else
    FINAL_PROMPT="$TASK"
fi

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
export COPILOT_MODEL="${MODEL:-${COPILOT_MODEL:-gpt-4o-mini}}"

# ---------------------------------------------------------------------------
# Run Copilot
# ---------------------------------------------------------------------------
# Create temp files under INSTALL_DIR/tmp/ rather than /tmp so that all
# process artefacts stay within the controlled installation directory.
# Falls back to a /tmp sub-directory when INSTALL_DIR/tmp/ is not writable —
# this covers two cases:
#   a) mkdir -p fails (parent not writable or path is a file)
#   b) mkdir succeeds (dir already exists) but mktemp fails (dir is read-only)
# In both cases the fallback is a dedicated sub-directory under /tmp, never
# a bare /tmp file, to avoid namespace collisions.
_INSTALL_DIR_BASE="${COPILOT_INSTALL_DIR:-${HOME}/.local/share/ai-agent/copilot}"
_PRIMARY_WRAPPER_TMP="${_INSTALL_DIR_BASE}/tmp"
_FALLBACK_WRAPPER_TMP="/tmp/copilot-agent-$$"
TMP_OUT=""
TMP_ERR=""
if ! create_temp_pair "$_PRIMARY_WRAPPER_TMP"; then
    if ! create_temp_pair "$_FALLBACK_WRAPPER_TMP"; then
        echo "ERROR: Failed to create temporary files for Copilot output" >&2
        exit 1
    fi
fi
_COPILOT_BGPID=""
cleanup() {
    # Kill Copilot if it is still running. When invoked via the MCP server,
    # Python's os.killpg() already kills the whole process group; this is
    # a defence-in-depth for direct wrapper invocations.
    if [[ -n "$_COPILOT_BGPID" ]]; then
        kill -- "$_COPILOT_BGPID" 2>/dev/null || true
    fi
    # Note: jobs -p is intentionally omitted here.  The wrapper is always
    # launched in a non-interactive shell (from Python or a terminal), so
    # the jobs table is either empty or unreliable.  _COPILOT_BGPID covers
    # the only background process this script ever starts.
    rm -f "$TMP_OUT" "${TMP_OUT}.sanitized" "$TMP_ERR"
}
trap cleanup EXIT

# Build tool-permission flags.
# In non-interactive mode Copilot requires --allow-all-tools, so we always pair
# it with --available-tools=... to constrain what is actually visible to the
# model. An explicit empty whitelist becomes --available-tools=, which disables
# all Copilot tools instead of widening permissions.
_COPILOT_TOOL_FLAGS=()
if [[ ${#ALLOWED_TOOLS[@]} -gt 0 ]]; then
    _tools_csv=""
    for _t in "${ALLOWED_TOOLS[@]}"; do
        _tools_csv="${_tools_csv}${_tools_csv:+,}${_t}"
    done
    _COPILOT_TOOL_FLAGS=("--available-tools=${_tools_csv}" "--allow-all-tools")
else
    _COPILOT_TOOL_FLAGS=("--available-tools=" "--allow-all-tools")
fi

# Run in background so _COPILOT_BGPID is available to cleanup on early exit.
run_with_timeout "$COPILOT_BIN" -p "$FINAL_PROMPT" "${_COPILOT_TOOL_FLAGS[@]}" >"$TMP_OUT" 2>"$TMP_ERR" &
_COPILOT_BGPID=$!
_copilot_rc=0
wait "$_COPILOT_BGPID" || _copilot_rc=$?
_COPILOT_BGPID=""  # process has exited; no need to kill in cleanup

if [[ "$_copilot_rc" -ne 0 ]]; then
    if [[ "$_copilot_rc" -eq 124 ]]; then
        echo "[$(ts)] ERROR: Copilot execution timed out after ${TIMEOUT_SECONDS}s" >&2
    else
        echo "[$(ts)] ERROR: Copilot execution failed (rc=${_copilot_rc})" >&2
        cat "$TMP_ERR" >&2
    fi
    exit 5
fi

# ---------------------------------------------------------------------------
# Sanitize output — redact common secret patterns directly from the temp file
# ---------------------------------------------------------------------------
# Process TMP_OUT with sed into a separate sanitized file to avoid loading
# potentially large LLM output into a shell variable (ARG_MAX / memory safety).
# Patterns use explicit [Xx] alternation for portable case-insensitivity.
# Limitation: sed processes line-by-line, so a secret token that an LLM happens
# to split across a newline will not be caught. This is an accepted trade-off;
# the wrapper is a best-effort safety net. A token on a single line (the vast
# majority of cases) is reliably redacted.
# ---------------------------------------------------------------------------
# Secret redaction + Unicode-safe truncation via the shared redact.py module.
# redact.py is the single source of truth for redaction patterns so that
# this wrapper and the MCP server never drift apart.
# Locate redact.py: same directory as this script (installed layout) or
# the sibling src/ directory (direct repo invocation during development/tests).
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REDACT_SCRIPT=""
for _candidate in \
    "${_SCRIPT_DIR}/redact.py" \
    "${_SCRIPT_DIR}/../src/redact.py"; do
    if [[ -f "$_candidate" ]]; then
        REDACT_SCRIPT="$(cd "$(dirname "$_candidate")" && pwd)/$(basename "$_candidate")"
        break
    fi
done

if [[ -z "$REDACT_SCRIPT" ]]; then
    echo "ERROR: redact.py not found next to wrapper or in ../src/" >&2
    exit 7
fi

"$python_bin" "$REDACT_SCRIPT" "$TMP_OUT" "$MAX_OUTPUT"
