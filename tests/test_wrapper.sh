#!/usr/bin/env bash
# Smoke tests for copilot_wrapper.sh
# Tests safety checks without actually calling Copilot CLI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="${SCRIPT_DIR}/../src/copilot_wrapper.sh"

PASS=0
FAIL=0

run_test() {
    local name="$1"
    local expected_rc="$2"
    shift 2
    local actual_rc=0
    "$@" >/dev/null 2>&1 || actual_rc=$?
    if [[ "$actual_rc" -eq "$expected_rc" ]]; then
        echo "  PASS  $name"
        ((PASS++)) || true
    else
        echo "  FAIL  $name (expected rc=$expected_rc, got rc=$actual_rc)"
        ((FAIL++)) || true
    fi
}

echo ""
echo "Running copilot_wrapper.sh smoke tests..."
echo ""

# Missing task argument
run_test "missing task argument" 1 \
    bash "$WRAPPER"

# Empty task (explicit "" after --)
run_test "empty task → exit 1" 1 \
    bash "$WRAPPER" -- ""

# Task too long (>5000 chars)
LONG_TASK="$(python3 -c "print('x' * 5001)")"
run_test "task too long → exit 2" 2 \
    bash "$WRAPPER" "$LONG_TASK"

# Control character in task
run_test "control char in task → exit 3" 3 \
    bash "$WRAPPER" "$(printf 'hello\x01world')"

# Dangerous pattern: rm -rf (patterns now injected via --blocked-pattern)
run_test "dangerous pattern rm -rf → exit 4" 4 \
    bash "$WRAPPER" --blocked-pattern "rm -rf" "please run rm -rf /tmp/test"

# Dangerous pattern: shutdown
run_test "dangerous pattern shutdown → exit 4" 4 \
    bash "$WRAPPER" --blocked-pattern "shutdown" "shutdown the server"

# Dangerous pattern: chmod 777
run_test "dangerous pattern chmod 777 → exit 4" 4 \
    bash "$WRAPPER" --blocked-pattern "chmod 777" "chmod 777 the file"

# Valid task should NOT exit with 2/3/4 (may fail at copilot call — that's fine)
rc=0; COPILOT_BIN="false" bash "$WRAPPER" "write a hello world function" >/dev/null 2>&1 || rc=$?
if [[ "$rc" -ne 2 && "$rc" -ne 3 && "$rc" -ne 4 ]]; then
    echo "  PASS  valid task passes safety checks"
    ((PASS++)) || true
else
    echo "  FAIL  valid task incorrectly blocked (rc=${rc})"
    ((FAIL++)) || true
fi

# --model and --prompt-prefix flags are accepted (no parse error)
rc=0; COPILOT_BIN="false" bash "$WRAPPER" --model gpt-4o --prompt-prefix "Be concise." "explain this" >/dev/null 2>&1 || rc=$?
if [[ "$rc" -ne 1 ]]; then
    echo "  PASS  --model and --prompt-prefix accepted"
    ((PASS++)) || true
else
    echo "  FAIL  --model/--prompt-prefix caused parse error"
    ((FAIL++)) || true
fi

# --timeout flag is accepted (no parse error)
rc=0; COPILOT_BIN="false" bash "$WRAPPER" --timeout 600 "explain this" >/dev/null 2>&1 || rc=$?
if [[ "$rc" -ne 1 ]]; then
    echo "  PASS  --timeout accepted"
    ((PASS++)) || true
else
    echo "  FAIL  --timeout caused parse error"
    ((FAIL++)) || true
fi

# --blocked-pattern from config blocks the pattern
run_test "--blocked-pattern custom pattern → exit 4" 4 \
    bash "$WRAPPER" --blocked-pattern "drop table" "please drop table users"

# --blocked-pattern: valid task not blocked
rc=0; COPILOT_BIN="false" bash "$WRAPPER" --blocked-pattern "drop table" "write a select query" >/dev/null 2>&1 || rc=$?
if [[ "$rc" -ne 4 ]]; then
    echo "  PASS  --blocked-pattern: safe task not blocked"
    ((PASS++)) || true
else
    echo "  FAIL  --blocked-pattern: safe task incorrectly blocked"
    ((FAIL++)) || true
fi

# Regression: wrapper must not depend on inherited python_bin from the parent shell
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
echo "ok"
FAKE_EOF
chmod +x "$FAKE_COPILOT"
PYBIN_INSTALL="$(mktemp -d)"
rc=0
pybin_out="$(env -u python_bin COPILOT_BIN="$FAKE_COPILOT" COPILOT_INSTALL_DIR="$PYBIN_INSTALL" \
    bash "$WRAPPER" "test task" 2>/dev/null)" || rc=$?
rm -f "$FAKE_COPILOT"
rm -rf "$PYBIN_INSTALL"
if [[ "$rc" -eq 0 ]] && printf '%s' "$pybin_out" | grep -q "^ok$"; then
    echo "  PASS  wrapper works without inherited python_bin env var"
    ((PASS++)) || true
else
    echo "  FAIL  wrapper depends on inherited python_bin env var (rc=$rc, got: $pybin_out)"
    ((FAIL++)) || true
fi

# Secret redaction: mock Copilot to emit an OpenAI key in its output
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
echo "result: sk-abcdefghijklmnopqrstuvwxyz12345678"
FAKE_EOF
chmod +x "$FAKE_COPILOT"
redacted_out="$(COPILOT_BIN="$FAKE_COPILOT" bash "$WRAPPER" "test task" 2>/dev/null || true)"
rm -f "$FAKE_COPILOT"
if printf '%s' "$redacted_out" | grep -q "sk-REDACTED"; then
    echo "  PASS  secret redaction: sk- key is redacted in output"
    ((PASS++)) || true
else
    echo "  FAIL  secret redaction: sk- key not redacted (got: $redacted_out)"
    ((FAIL++)) || true
fi

# Unicode-safe truncation: do not split a multi-byte UTF-8 character
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
python3 - <<'PY_EOF'
print("\u017ex", end="")
PY_EOF
FAKE_EOF
chmod +x "$FAKE_COPILOT"
truncated_out="$(COPILOT_BIN="$FAKE_COPILOT" bash "$WRAPPER" --max-output 1 "test task" 2>/dev/null || true)"
rm -f "$FAKE_COPILOT"
if printf '%s' "$truncated_out" | grep -q "^ž$"; then
    echo "  PASS  unicode truncation preserves full characters"
    ((PASS++)) || true
else
    echo "  FAIL  unicode truncation split UTF-8 character (got: $truncated_out)"
    ((FAIL++)) || true
fi

# Regression: Bearer token spanning chunk boundary must be fully redacted
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
python3 -c "import sys; sys.stdout.write('x'*3500 + 'Bearer ' + 'A'*700)"
FAKE_EOF
chmod +x "$FAKE_COPILOT"
rc=0; bearer_out="$(COPILOT_BIN="$FAKE_COPILOT" bash "$WRAPPER" "test task" 2>/dev/null)" || rc=$?
rm -f "$FAKE_COPILOT"
if printf '%s' "$bearer_out" | grep -q "Bearer REDACTED" && \
   ! printf '%s' "$bearer_out" | grep -qE "A{50,}"; then
    echo "  PASS  Bearer token spanning chunk boundary fully redacted"
    ((PASS++)) || true
else
    echo "  FAIL  Bearer token not fully redacted (leaked portion present)"
    ((FAIL++)) || true
fi

# Regression: output of exactly max_output length must NOT get truncation marker
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
python3 -c "print('x' * 100, end='')"
FAKE_EOF
chmod +x "$FAKE_COPILOT"
exact_out="$(COPILOT_BIN="$FAKE_COPILOT" bash "$WRAPPER" --max-output 100 "test task" 2>/dev/null || true)"
rm -f "$FAKE_COPILOT"
if ! printf '%s' "$exact_out" | grep -q '\.\.\.\[truncated\]'; then
    echo "  PASS  exact-length output not marked as truncated"
    ((PASS++)) || true
else
    echo "  FAIL  exact-length output falsely marked as truncated"
    ((FAIL++)) || true
fi

# Wrapper should find copilot in ~/.local/bin after extending PATH
FAKE_HOME="$(mktemp -d)"
mkdir -p "$FAKE_HOME/.local/bin"
cat > "$FAKE_HOME/.local/bin/copilot" <<'FAKE_EOF'
#!/usr/bin/env bash
echo "copilot from home bin"
FAKE_EOF
chmod +x "$FAKE_HOME/.local/bin/copilot"
path_out="$(HOME="$FAKE_HOME" PATH="/usr/bin:/bin" bash "$WRAPPER" "test task" 2>/dev/null || true)"
rm -rf "$FAKE_HOME"
if printf '%s' "$path_out" | grep -q "copilot from home bin"; then
    echo "  PASS  wrapper finds copilot in ~/.local/bin"
    ((PASS++)) || true
else
    echo "  FAIL  wrapper did not find copilot in ~/.local/bin (got: $path_out)"
    ((FAIL++)) || true
fi

# --allowed-tool flag accepted (tool whitelist wired through to Copilot)
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
# Echo the flags we received so the test can inspect them
echo "args: $*"
FAKE_EOF
chmod +x "$FAKE_COPILOT"
tool_out="$(COPILOT_BIN="$FAKE_COPILOT" bash "$WRAPPER" \
    --allowed-tool view --allowed-tool grep -- "test task" 2>/dev/null || true)"
rm -f "$FAKE_COPILOT"
if printf '%s' "$tool_out" | grep -q "available-tools=view,grep"; then
    echo "  PASS  --allowed-tool builds --available-tools CSV"
    ((PASS++)) || true
else
    echo "  FAIL  --allowed-tool not wired (got: $tool_out)"
    ((FAIL++)) || true
fi

# Function-call rhs (password=input(...), token=request.get(...))
# must pass through completely unmodified — the whole expression must survive.
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
echo 'password=input("enter password: ")'
echo 'token=request.headers.get("Authorization")'
FAKE_EOF
chmod +x "$FAKE_COPILOT"
code_out="$(COPILOT_BIN="$FAKE_COPILOT" bash "$WRAPPER" "test task" 2>/dev/null || true)"
rm -f "$FAKE_COPILOT"
# Both expressions must be present unchanged (the whole lhs=rhs, not just the parens)
if printf '%s' "$code_out" | grep -q 'password=input("enter password: ")' && \
   printf '%s' "$code_out" | grep -q 'token=request.headers.get("Authorization")'; then
    echo "  PASS  code redaction: function-call rhs not redacted"
    ((PASS++)) || true
else
    echo "  FAIL  code redaction: function-call expression corrupted (got: $code_out)"
    ((FAIL++)) || true
fi

# Empty whitelist: no --allowed-tool flags → disable all tools via --available-tools=
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
echo "args: $*"
FAKE_EOF
chmod +x "$FAKE_COPILOT"
h5_out="$(COPILOT_BIN="$FAKE_COPILOT" bash "$WRAPPER" "test task" 2>/dev/null || true)"
rm -f "$FAKE_COPILOT"
if printf '%s' "$h5_out" | grep -q -- "--available-tools=" && \
   ! printf '%s' "$h5_out" | grep -q -- "--available-tools=view"; then
    echo "  PASS  empty whitelist disables all tools"
    ((PASS++)) || true
else
    echo "  FAIL  empty whitelist did not disable tools (got: $h5_out)"
    ((FAIL++)) || true
fi

# Non-empty whitelist still builds a concrete CSV
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
echo "args: $*"
FAKE_EOF
chmod +x "$FAKE_COPILOT"
h5_tools="$(COPILOT_BIN="$FAKE_COPILOT" bash "$WRAPPER" --allowed-tool view "test task" 2>/dev/null || true)"
rm -f "$FAKE_COPILOT"
if printf '%s' "$h5_tools" | grep -q -- "--available-tools=view"; then
    echo "  PASS  non-empty whitelist forwarded to Copilot"
    ((PASS++)) || true
else
    echo "  FAIL  non-empty whitelist missing from Copilot args (got: $h5_tools)"
    ((FAIL++)) || true
fi

# github_pat_ token is redacted
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
echo "token: github_pat_abcdefghij1234567890ABCDEFGH"
FAKE_EOF
chmod +x "$FAKE_COPILOT"
h6_out="$(COPILOT_BIN="$FAKE_COPILOT" bash "$WRAPPER" "test task" 2>/dev/null || true)"
rm -f "$FAKE_COPILOT"
if printf '%s' "$h6_out" | grep -q "REDACTED" && \
   ! printf '%s' "$h6_out" | grep -q "github_pat_abcdefghij1234567890"; then
    echo "  PASS  github_pat_ token redacted"
    ((PASS++)) || true
else
    echo "  FAIL  github_pat_ token not redacted (got: $h6_out)"
    ((FAIL++)) || true
fi

# Slack xoxb- token is redacted
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
echo "slack: xoxb-123456789012-abcdefghijklmnopqrst"
FAKE_EOF
chmod +x "$FAKE_COPILOT"
slack_out="$(COPILOT_BIN="$FAKE_COPILOT" bash "$WRAPPER" "test task" 2>/dev/null || true)"
rm -f "$FAKE_COPILOT"
if printf '%s' "$slack_out" | grep -q "REDACTED" && \
   ! printf '%s' "$slack_out" | grep -q "xoxb-123456789012-abcdefghijklmnopqrst"; then
    echo "  PASS  Slack xoxb- token redacted"
    ((PASS++)) || true
else
    echo "  FAIL  Slack xoxb- token not redacted (got: $slack_out)"
    ((FAIL++)) || true
fi

# PostgreSQL URL with embedded password is redacted
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
echo "conn = postgresql://user:supersecret@host:5432/mydb"
FAKE_EOF
chmod +x "$FAKE_COPILOT"
db_out="$(COPILOT_BIN="$FAKE_COPILOT" bash "$WRAPPER" "test task" 2>/dev/null || true)"
rm -f "$FAKE_COPILOT"
if printf '%s' "$db_out" | grep -q "REDACTED" && \
   ! printf '%s' "$db_out" | grep -q "supersecret"; then
    echo "  PASS  PostgreSQL URL with password redacted"
    ((PASS++)) || true
else
    echo "  FAIL  PostgreSQL URL not redacted (got: $db_out)"
    ((FAIL++)) || true
fi

# Cross-check — both server and wrapper redact an sk- key identically
# The wrapper uses inline Python; verify a canonical sk- key is redacted there too.
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
echo "key: sk-abcdefghijklmnopqrstuvwxyz12345678"
FAKE_EOF
chmod +x "$FAKE_COPILOT"
l1_out="$(COPILOT_BIN="$FAKE_COPILOT" bash "$WRAPPER" "test task" 2>/dev/null || true)"
rm -f "$FAKE_COPILOT"
# Server returns "sk-REDACTED"; wrapper returns "sk-REDACTED" too (same prefix replacement)
if printf '%s' "$l1_out" | grep -q "sk-REDACTED" && \
   ! printf '%s' "$l1_out" | grep -q "sk-abcdefghij"; then
    echo "  PASS  sk- key redacted to sk-REDACTED (cross-check server==wrapper)"
    ((PASS++)) || true
else
    echo "  FAIL  sk- key not redacted as expected (got: $l1_out)"
    ((FAIL++)) || true
fi

# --allowed-tool with comma → exit 1
rc=0; bash "$WRAPPER" --allowed-tool "view,bash" "test task" >/dev/null 2>&1 || rc=$?
if [[ "$rc" -eq 1 ]]; then
    echo "  PASS  --allowed-tool with comma rejected → exit 1"
    ((PASS++)) || true
else
    echo "  FAIL  --allowed-tool with comma not rejected (rc=${rc})"
    ((FAIL++)) || true
fi

# Cyrillic е (U+0435) in "sеcurity" still blocked by normalize
# The wrapper doesn't do keyword matching — that's the server's job.
# This test verifies wrapper correctly forwards the homoglyph task to Copilot
# (doesn't block it — the wrapper is not the policy gate for keywords).
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
echo "ok"
FAKE_EOF
chmod +x "$FAKE_COPILOT"
# Task with Cyrillic е (U+0435) — wrapper should NOT block (only server classify_task does).
# Generate the Cyrillic string via Python because bash printf does not support
# \uXXXX Unicode escapes in the format string, and $'\uXXXX' ANSI-C quoting
# is not available in macOS's default bash 3.2.
CYRILLIC_TASK="$(python3 -c "import sys; sys.stdout.write('s\u0435curity review')")"
rc=0; COPILOT_BIN="$FAKE_COPILOT" bash "$WRAPPER" "$CYRILLIC_TASK" >/dev/null 2>&1 || rc=$?
rm -f "$FAKE_COPILOT"
if [[ "$rc" -ne 4 ]]; then
    echo "  PASS  Cyrillic homoglyph task passes wrapper (keyword check is server-side)"
    ((PASS++)) || true
else
    echo "  FAIL  wrapper incorrectly blocked Cyrillic task (rc=${rc})"
    ((FAIL++)) || true
fi

# Wrapper must still succeed when INSTALL_DIR/tmp exists but is not writable
FAKE_COPILOT="$(mktemp "${TMPDIR:-/tmp}/fake_copilot_XXXXXX")"
cat > "$FAKE_COPILOT" <<'FAKE_EOF'
#!/usr/bin/env bash
echo "ok"
FAKE_EOF
chmod +x "$FAKE_COPILOT"
H1_INSTALL="$(mktemp -d)"
mkdir -p "$H1_INSTALL/tmp"
chmod 500 "$H1_INSTALL/tmp"
h1_out="$(COPILOT_BIN="$FAKE_COPILOT" COPILOT_INSTALL_DIR="$H1_INSTALL" \
    bash "$WRAPPER" "test task" 2>/dev/null || true)"
chmod 700 "$H1_INSTALL/tmp"
rm -f "$FAKE_COPILOT"
# Verify the wrapper fell back cleanly instead of crashing with mktemp permission errors.
if printf '%s' "$h1_out" | grep -q "^ok$"; then
    echo "  PASS  read-only INSTALL_DIR/tmp falls back cleanly"
    ((PASS++)) || true
else
    echo "  FAIL  read-only INSTALL_DIR/tmp broke wrapper (got: $h1_out)"
    ((FAIL++)) || true
fi
rm -rf "$H1_INSTALL"

# Exit code 6 — Copilot binary not found
rc=0; COPILOT_BIN=/nonexistent/copilot bash "$WRAPPER" "test task" >/dev/null 2>&1 || rc=$?
if [[ "$rc" -eq 6 ]]; then
    echo "  PASS  exit 6 when Copilot binary not found"
    ((PASS++)) || true
else
    echo "  FAIL  expected exit 6 for missing Copilot binary (got rc=$rc)"
    ((FAIL++)) || true
fi

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
echo ""
[[ "$FAIL" -eq 0 ]]
