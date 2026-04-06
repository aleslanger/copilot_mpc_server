# Code Review — ai_agent_copilot

---

## Review 2026-04-05

Reviewed files: `src/copilot_mcp_server.py`, `src/copilot_wrapper.sh`,
`install-copilot-agent.sh`, `tests/test_mcp_server.py`, `tests/test_wrapper.sh`,
`config.yaml`, `README.md`, `CLAUDE.md`

Model: Claude Sonnet 4.6

**Status legend:** 🔲 Open | ⚠️ Accepted/Known

---

## High Priority

### H1. Wrapper redaction pattern: `eyJ`, `ya29.`, `AccountKey` thresholds too high — `src/copilot_wrapper.sh:279-280`

**Severity:** High  
**Category:** Secret leakage

The inline Python redaction block uses `{20,}` for `eyJ` (JWT/GCP service-account tokens) and `ya29.` (GCP OAuth), and `{20,}` for `AccountKey=`. The server's `_LOG_REDACT_RE` uses `{10,}` for all three. A real GCP OAuth access token always has a long suffix, but an eyJ JWT header payload can be as short as 12–15 characters at the base64 level (e.g. `eyJhbGciOiJSUzI1`). Any such short token **passes through the wrapper unredacted** to the MCP client, even though the server log correctly redacts it.

Reproduction:

```python
import re
wrapper_pattern = re.compile(
    r"eyJ[A-Za-z0-9._-]{20,}",
    re.IGNORECASE | re.DOTALL,
)
tok = "eyJ" + "a" * 15        # 15 chars after eyJ: real short JWT header
print(wrapper_pattern.search(tok))  # None — NOT redacted
```

**Fix:** Align wrapper thresholds with the server's `{10,}`:

```python
# copilot_wrapper.sh lines 279-280, change:
r"|Bearer\s+[A-Za-z0-9._~+/=-]{8,}|eyJ[A-Za-z0-9._-]{20,}"
r"|ya29\.[A-Za-z0-9._-]{20,}|AccountKey=[A-Za-z0-9+/]{20,}=*"
# to:
r"|Bearer\s+[A-Za-z0-9._~+/=-]{8,}|eyJ[A-Za-z0-9._-]{10,}"
r"|ya29\.[A-Za-z0-9._-]{10,}|AccountKey=[A-Za-z0-9+/]{10,}=*"
```

Status: 🔲 Open

---

### H2. Server `_LOG_REDACT_RE` misses `Bearer` with TAB separator — `src/copilot_mcp_server.py:105`

**Severity:** High  
**Category:** Secret leakage

The server's log-redaction pattern uses a fixed-space lookbehind:

```python
r"|(?<=Bearer )[A-Za-z0-9._~+/=-]{10,}"
```

The lookbehind requires exactly one space character before the token. HTTP headers can use a tab (`\t`) as whitespace after `Bearer` (RFC 7230 allows OWS). If Copilot emits `Authorization: Bearer\t<token>` in its output (e.g. by printing a raw header), the server's `_redact()` function on log entry leaves the raw token in the log file.

The wrapper handles this correctly with `Bearer\s+` which matches any whitespace.

Reproduction:

```python
import re
server = re.compile(r"(?<=Bearer )[A-Za-z0-9._~+/=-]{10,}", re.IGNORECASE)
print(server.search("Bearer\tabcdefghijklmn"))  # None — LEAKED IN LOG
```

**Fix:** Replace the lookbehind with a non-capturing group to allow `\s+`:

```python
# change:
r"|(?<=Bearer )[A-Za-z0-9._~+/=-]{10,}"
# to:
r"|(?:Bearer\s+)([A-Za-z0-9._~+/=-]{10,})"
```

Note: if the match group is changed, `_redact()`'s `re.sub` replacement must be updated to reconstruct `Bearer REDACTED` instead of emitting `[REDACTED]` for the whole string. Simpler alternative: use `re.sub` with a lambda that checks the match context, or keep the lookbehind but extend it to `(?<=Bearer\s)` (which still only allows one char). The cleanest fix is to replace the lookbehind altogether:

```python
r"|Bearer\s+[A-Za-z0-9._~+/=-]{10,}"
```

and update `_redact()` to produce `Bearer REDACTED` rather than `[REDACTED]` for this case.

Status: 🔲 Open

---

### H3. Wrapper redaction false-positives corrupt legitimate LLM output — `src/copilot_wrapper.sh:282`

**Severity:** High (correctness — not security)  
**Category:** Correctness / output corruption

The generic secret pattern in the wrapper:

```python
r"|(?:api[_-]?key|token|secret|password|passwd)=[^\s,;'\"]+"
```

The negated class `[^\s,;'\"]+` stops at whitespace and quotes but **not** at parentheses, brackets, or other punctuation. When Copilot returns code examples — which is the primary use-case — patterns like `password=input(...)`, `token=request.headers.get(...)`, or `secret=os.environ[...]` are incorrectly redacted:

```
INPUT:  password=input("Enter password")
OUTPUT: password=REDACTED"Enter password")
```

```
INPUT:  token=request.headers.get("Authorization")
OUTPUT: token=REDACTED"Authorization")
```

This produces syntactically broken Python that Claude then silently forwards to the user as the "corrected" code. The issue exists in the wrapper's output path (the Python block at lines 271–313) but not in the server's log path (because `_redact` is only called on error/warning text, not on the successful stdout response — the server returns `out` directly from `_decode(stdout).strip()` at line 426, so the server does not redact the successful response at all; redaction is wrapper-only for output).

Reproduction — run this against the wrapper with a fake copilot:

```bash
cat > /tmp/fake_copilot.sh << 'EOF'
#!/usr/bin/env bash
echo 'def login(user, password=input("Enter password: ")):'
echo '    token=request.headers.get("Authorization")'
EOF
chmod +x /tmp/fake_copilot.sh
COPILOT_BIN=/tmp/fake_copilot.sh bash src/copilot_wrapper.sh "test task"
# Output contains: password=REDACTED"Enter password: "):
#                   token=REDACTED"Authorization")
```

**Fix options (in order of preference):**

1. Add `([` to the stop-set: `[^\s,;'\"([{]+` — this stops before function calls and subscripts.
2. Require at least one non-word character after `=` before stopping: use `=[A-Za-z0-9_\-\.]{8,}` to match only plausible secret values (no parens, no dots after short values).
3. Accept as a known limitation and document it; it is already somewhat documented as "best-effort".

Minimum change (option 1):

```python
# change:
r"|(?:api[_-]?key|token|secret|password|passwd)=[^\s,;'\"]+"
# to:
r"|(?:api[_-]?key|token|secret|password|passwd)=[^\s,;'\"([{]+"
```

Status: 🔲 Open

---

## Medium Priority

### M1. `_check_not_sudo` fails open when `getent` and `dscl` are both unavailable — `install-copilot-agent.sh:823-844`

**Severity:** Medium  
**Category:** Robustness / usability

`_check_not_sudo` resolves the real user's home via `resolve_user_home()`. On a minimal container or Alpine Linux, neither `getent` nor `dscl` is present, so `resolve_user_home` returns exit code 1 and `sudo_home` is set to an empty string via `|| true`. The guard condition is:

```bash
if [[ -n "$sudo_home" && "${INSTALL_DIR}" == "${sudo_home}"* ]]; then
```

With an empty `sudo_home`, the `[[ -n "" ]]` branch is false, and the entire safety check is silently skipped. A `sudo ./install-copilot-agent.sh` on a container where `getent` is absent will install files owned by `root` under the default `${HOME}/.local/...` without any warning.

**Fix:** Add a fallback using `/etc/passwd`:

```bash
resolve_user_home() {
    local username="$1"
    [[ -n "$username" && "$username" =~ ^[A-Za-z0-9._-]+$ ]] || return 1
    if command -v getent &>/dev/null; then
        getent passwd "$username" 2>/dev/null | cut -d: -f6
        return 0
    fi
    if command -v dscl &>/dev/null; then
        dscl . -read "/Users/${username}" NFSHomeDirectory 2>/dev/null | awk '{print $2}'
        return 0
    fi
    # POSIX fallback: parse /etc/passwd directly
    local h
    h="$(grep -m1 "^${username}:" /etc/passwd 2>/dev/null | cut -d: -f6 || true)"
    [[ -n "$h" ]] && printf '%s\n' "$h" && return 0
    return 1
}
```

Alternatively, if the fallback is not added, `_check_not_sudo` should treat an empty `sudo_home` as a reason to warn (not silently pass):

```bash
if [[ -z "$sudo_home" ]]; then
    warn "Could not resolve home directory for '${SUDO_USER}'. Proceeding with caution."
fi
```

Status: 🔲 Open

---

### M2. State file written to `/root/.local/...` when `resolve_user_home` fails under `sudo` — `install-copilot-agent.sh:114-122`

**Severity:** Medium  
**Category:** Robustness

`_state_file_path()` uses `resolve_user_home` to determine the real user's home under `sudo`. If `resolve_user_home` fails (returns 1, produces empty `sh`), `user_home` stays as `$HOME` — which is `/root` when running under `sudo`. The state file is then written to `/root/.local/share/ai-agent/copilot-install-dir`.

When the real user then runs `./install-copilot-agent.sh --status` or `--update`, `_state_file_path()` returns a path under their own home (e.g. `/home/alice/.local/...`), the state file is not found, and `load_install_dir()` silently does nothing. The `--status` then reports the default install path rather than the actual installed one.

This is a direct consequence of the same root cause as M1.

**Fix:** The same `/etc/passwd` fallback as M1 resolves both issues. Additionally, `_state_file_path` could emit a warning to stderr when it cannot determine the real user's home:

```bash
if [[ -n "$sh" ]]; then
    user_home="$sh"
else
    echo "[WARN] Could not resolve home for '${SUDO_USER}', state file may be misplaced" >&2
fi
```

Status: 🔲 Open

---

### M3. `asyncio.Semaphore` created at module level — Python 3.10+ only safe — `src/copilot_mcp_server.py:309`

**Severity:** Medium (portability)  
**Category:** Portability / correctness

```python
_global_semaphore = asyncio.Semaphore(_DEFAULT_MAX_CONCURRENCY)
```

This is created at import time, before any event loop exists. In Python 3.10+, `asyncio` primitives are not bound to a loop at creation time and are lazily attached to the running loop on first use — this is safe. In Python 3.9 and earlier, `asyncio.Semaphore()` binds to the running loop at creation time; at module load time there is no running loop, causing a `DeprecationWarning` (3.8–3.9) and a `RuntimeError` in some configurations.

The `pyproject.toml` specifies `requires-python = ">=3.11"` and the installer enforces 3.11+, so this is not a bug in the intended environment. However, the server file itself carries no `# requires Python 3.10+` comment explaining why this is safe.

**Fix:** Add an explicit comment, or guard the semaphore creation inside `main()` where a loop is guaranteed to exist:

```python
def main() -> None:
    global _global_semaphore
    _global_semaphore = asyncio.Semaphore(_DEFAULT_MAX_CONCURRENCY)
    _register_tools()
    mcp.run(transport="stdio")
```

Alternatively, keep it at module level and add:

```python
# Safe: Python 3.10+ creates asyncio primitives without binding to a loop at
# construction time; they attach lazily to the running loop on first use.
_global_semaphore = asyncio.Semaphore(_DEFAULT_MAX_CONCURRENCY)
```

Status: ⚠️ Accepted (requires Python 3.11+ per pyproject.toml; document it)

---

### M4. `run_with_timeout` Python fallback does not propagate signals to child — `src/copilot_wrapper.sh:138-148`

**Severity:** Medium  
**Category:** Robustness / orphan processes

The Python fallback for `timeout` (used when neither GNU `timeout` nor `gtimeout` is available):

```python
completed = subprocess.run(command, timeout=timeout_seconds)
```

`subprocess.run()` with a `timeout` raises `TimeoutExpired` but does **not** kill the child process automatically in Python < 3.11. In Python 3.11+, `subprocess.run()` still does not kill the child on timeout — `TimeoutExpired` is raised and the child continues running. The inline Python does `sys.exit(124)` immediately after catching `TimeoutExpired`, leaving the Copilot subprocess orphaned.

The same wrapper also notes `COPILOT_BIN` is run in background so `_COPILOT_BGPID` can be killed by `cleanup()`. But the Python fallback for `timeout` runs `subprocess.run` (which is a blocking foreground call), so the Copilot process that goes overtime is a grandchild of the bash script not tracked by `jobs -p`. After `sys.exit(124)`, the grandchild process continues running with no owner.

**Fix:**

```python
import subprocess, sys, os, signal
timeout_seconds = int(sys.argv[1])
command = sys.argv[2:]
try:
    completed = subprocess.run(command, timeout=timeout_seconds)
except subprocess.TimeoutExpired as e:
    try:
        os.killpg(os.getpgid(e.process.pid), signal.SIGTERM)
    except (ProcessLookupError, AttributeError, OSError):
        pass
    sys.exit(124)
sys.exit(completed.returncode)
```

Note: `e.process` is available in Python 3.5+. `os.getpgid` + `killpg` handles the whole process group.

Status: 🔲 Open

---

### M5. `wrapper` calls `copilot` with empty TASK string when invoked directly — `src/copilot_wrapper.sh:75-80`

**Severity:** Medium (correctness, wrapper API contract)  
**Category:** Correctness

The wrapper checks `$# -lt 1` (no arguments at all) and exits 1. But if the caller passes an empty string as the task argument (`bash copilot_wrapper.sh --`), `$#` is 0 after the `--` `shift`, so that case is caught correctly. However, if the caller passes `bash copilot_wrapper.sh -- ""`, `$#` is 1 and `TASK=""`. The length check `${#TASK} -gt MAX_INPUT_LENGTH` is false for 0 characters, so Copilot is invoked with `-p ""`. The Copilot CLI behavior with an empty prompt is unspecified and may hang or return an error.

The Python MCP server guards `if not task or not task.strip()` before calling the wrapper, so this cannot happen through the normal code path. But it is an undocumented edge case for direct wrapper invocations.

**Fix:**

```bash
TASK="$1"
if [[ -z "$TASK" ]]; then
    echo "ERROR: Empty task" >&2
    exit 1
fi
```

Status: 🔲 Open

---

## Low Priority

### L1. Redaction pattern inconsistency documented but not tracked — both files

**Severity:** Low  
**Category:** Maintainability

There are now two separate copies of the redaction regex: one in `copilot_mcp_server.py` (`_LOG_REDACT_RE`, applies to log writes and error strings returned to client) and one in the inline Python block inside `copilot_wrapper.sh` (applies to Copilot stdout). They have diverged in minimum-length thresholds (H1 above), Bearer syntax (H2 above), and DOTALL flag (wrapper has it, server does not need it since server never processes multi-line output directly). There is no test that asserts both patterns are equivalent.

**Fix:** Extract the canonical pattern into a single shared Python module (e.g. `src/redact.py`) imported by the server. The wrapper's inline Python block could `import` it from the installed path, or the pattern string could be generated from a single authoritative source at install time.

Alternatively, add a cross-check test in `test_mcp_server.py` that instantiates both patterns and verifies they produce identical results on a standard set of test inputs.

Status: 🔲 Open

---

### L2. `install_python`: Arch Linux and openSUSE branches do not set `pkg_installed=1` — `install-copilot-agent.sh:355-360`

**Severity:** Low  
**Category:** Correctness / consistency

All Debian/Fedora/RHEL branches set `pkg_installed=1` on success. The Arch and SUSE branches do not:

```bash
*arch*|*manjaro*)
    run_privileged pacman -Sy --noconfirm python python-pip
    ;;
*suse*|*opensuse*)
    run_privileged zypper install -y python3 python3-pip python3-venv
    ;;
```

The post-install guard (line 374) only checks `(debian|ubuntu|fedora)`, so it does not fire for Arch/SUSE anyway. Functionally the code is safe because `find_python` is always called after, and that will `die` if Python is still not present. But the inconsistency is confusing to future maintainers.

**Fix:**

```bash
*arch*|*manjaro*)
    run_privileged pacman -Sy --noconfirm python python-pip
    pkg_installed=1
    ;;
*suse*|*opensuse*)
    run_privileged zypper install -y python3 python3-pip python3-venv
    pkg_installed=1
    ;;
```

Status: 🔲 Open

---

### L3. No tests covering async handler code path — `tests/test_mcp_server.py`

**Severity:** Low  
**Category:** Test coverage

`make_handler()` contains the core runtime logic: subprocess launch, timeout escalation (SIGTERM → SIGKILL), return-code handling, empty-output detection, and semaphore acquisition. None of this is tested. The existing pytest suite covers only `classify_task`, `load_config`, `sanitize_profile_name`, and `_redact`.

Missing test scenarios:
- Handler returns `"ERROR: Empty task"` for blank input.
- Handler returns `"ERROR: Task rejected by policy"` for rejected keyword.
- Handler returns `"ERROR: Wrapper not found"` when `WRAPPER` path does not exist.
- Handler propagates wrapper non-zero exit as `"ERROR: Copilot failed: ..."`.
- Handler returns `"ERROR: Copilot wrapper timeout"` on timeout.
- Handler returns `"ERROR: Empty Copilot response"` on empty stdout.
- Semaphore correctly limits concurrency to `_DEFAULT_MAX_CONCURRENCY`.

**Fix:** Add an `asyncio`-based test file using `pytest-asyncio` and a mock wrapper script:

```python
# tests/test_handler.py
import asyncio
import pytest
from unittest.mock import patch

@pytest.mark.asyncio
async def test_empty_task_rejected(tmp_path):
    handler = make_handler("simple", {"rejected_keywords": [], "blocked_patterns": []})
    with patch("copilot_mcp_server.WRAPPER", str(tmp_path / "nonexistent")):
        result = await handler("")
    assert result == "ERROR: Empty task"
```

Status: 🔲 Open

---

### L4. `README.md` test count is out of date — `README.md`

**Severity:** Low  
**Category:** Documentation

`README.md` does not state a specific test count, but `CLAUDE.md` and earlier review notes reference the count diverging. The README instructions say `pytest` but do not mention the bash test suite in the CI/run-tests section — a developer following the docs would miss `bash tests/test_wrapper.sh`.

**Fix:** Add to the "Running Tests" section:

```bash
# Run all tests
pytest                      # 48 Python unit tests
bash tests/test_wrapper.sh  # 16 bash smoke tests
```

Status: 🔲 Open

---

## Summary

| Severity | Count | Key areas |
|----------|-------|-----------|
| High     | 3     | Redaction threshold mismatch (eyJ/ya29/AccountKey), Bearer TAB bypass in server log, false-positive redaction corrupts LLM code output |
| Medium   | 5     | `_check_not_sudo` fails open on minimal containers, state file misplaced under sudo, asyncio.Semaphore portability note, Python timeout fallback orphans child, empty TASK not guarded in wrapper |
| Low      | 4     | Dual-pattern drift without shared source, Arch/SUSE `pkg_installed` inconsistency, no async handler tests, README test instructions incomplete |

All High items are bugs with concrete reproduction cases. H3 (false-positive redaction) directly affects the primary use-case: Copilot returning code snippets with variable assignments.

---

## Review 2026-04-06 — Multi-Model

Reviewed files: `src/copilot_mcp_server.py`, `src/copilot_wrapper.sh`,
`tests/test_mcp_server.py`, `tests/test_wrapper.sh`

Models: Claude Opus 4.5 (security), GPT-5.2 (code quality), Claude Sonnet 4.5 (architecture)

Existing tests confirmed passing: 54 pytest + 19 wrapper smoke tests.

**Status legend:** 🔲 Open | ⚠️ Accepted/Known

---

## Critical Priority

### C1. `COPILOT_INSTALL_DIR` env var umožňuje načtení útočníkova `config.yaml` — `src/copilot_mcp_server.py:18-33`

**Severity:** Critical
**Category:** Security — access control bypass
**Model:** Claude Opus 4.5

Útočník nastaví `COPILOT_INSTALL_DIR=/tmp/evil` a umístí `config.yaml` s prázdnými `blocked_patterns` a `rejected_keywords`. Server načte útočníkův config a obejde veškerá bezpečnostní pravidla.

```python
INSTALL_DIR = os.environ.get(
    "COPILOT_INSTALL_DIR",
    str(Path.home() / ".local/share/ai-agent/copilot"),
)
# CONFIG_FILE = INSTALL_DIR/config.yaml  ← útočník kontroluje tuto cestu
```

**Fix:** Validovat `COPILOT_INSTALL_DIR` proti whitelistu povolených prefixů:

```python
def _validate_install_dir(path: str) -> bool:
    resolved = Path(path).resolve()
    allowed = [Path.home(), Path("/usr/local/share"), Path("/opt")]
    return any(resolved == p or p in resolved.parents for p in allowed)

_raw_install_dir = os.environ.get("COPILOT_INSTALL_DIR")
if _raw_install_dir:
    if not _validate_install_dir(_raw_install_dir):
        print(f"[ERROR] COPILOT_INSTALL_DIR points to untrusted location: {_raw_install_dir}", file=sys.stderr)
        sys.exit(1)
    INSTALL_DIR = _raw_install_dir
else:
    INSTALL_DIR = str(Path.home() / ".local/share/ai-agent/copilot")
```

Status: 🔲 Open

---

### C2. `asyncio.CancelledError` není odchycen → orphaned subprocesy — `src/copilot_mcp_server.py:386-451`

**Severity:** Critical (reliability + resource leakage)
**Category:** Bug — process lifecycle
**Model:** GPT-5.2

`CancelledError` dědí z `BaseException`, ne z `Exception`. Když klient zruší request, handler skočí přes `finally: _global_semaphore.release()` bez ukončení process group. Výsledkem jsou orphaned wrapper/Copilot procesy a porušení concurrency limitu.

**Fix:** Přidat explicitní `except asyncio.CancelledError` větev:

```python
except asyncio.CancelledError:
    if proc is not None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            await proc.wait()
    raise  # re-raise CancelledError
```

Status: 🔲 Open

---

## High Priority

### H4. `load_config()` nahrazuje DEFAULT_CONFIG místo merge — `src/copilot_mcp_server.py:243-310`

**Severity:** High
**Category:** Correctness / usability
**Model:** GPT-5.2

Pokud uživatel definuje v `config.yaml` jen jeden profil, ostatní defaultní profily (`security`, `code_review`) zmizí. Chování se výrazně mění na základě partial configu.

**Fix:** Mergovat user profily s `DEFAULT_CONFIG["profiles"]`:

```python
merged_profiles = dict(DEFAULT_CONFIG["profiles"])
merged_profiles.update(valid_profiles)
data["profiles"] = merged_profiles
return data
```

Status: 🔲 Open

---

### H5. `allowed_tools` prázdné → `--allow-all-tools` (nebezpečný fallback) — `src/copilot_wrapper.sh:261-269`

**Severity:** High
**Category:** Security — accidental privilege expansion
**Model:** GPT-5.2

Pokud profil nemá definované `allowed_tools`, wrapper použije `--allow-all-tools` bez omezení. Špatně nakonfigurovaný custom profil tak získá plná práva.

```bash
if [[ ${#ALLOWED_TOOLS[@]} -gt 0 ]]; then
    # omezený set nástrojů
else
    _COPILOT_TOOL_FLAGS=("--allow-all-tools")  # ← všechna práva
fi
```

**Fix:** Zvážit změnu defaultu na "žádné auto-approval" nebo vyžadovat explicitní `allowed_tools` v každém profilu. Minimální fix — přidat varování:

```bash
else
    echo "[WARNING] No allowed_tools configured — all tools auto-approved" >&2
    _COPILOT_TOOL_FLAGS=("--allow-all-tools")
fi
```

Status: 🔲 Open

---

### H6. Neúplná redakce secrets — chybí moderní formáty — `src/copilot_mcp_server.py:104-116` a `src/copilot_wrapper.sh:311-316`

**Severity:** High
**Category:** Secret leakage
**Model:** Claude Opus 4.5

Chybí vzory pro:
- GitHub fine-grained PAT: `github_pat_[A-Za-z0-9]{20,}`
- GitHub OAuth token: `gho_[A-Za-z0-9]{10,}`
- Slack tokeny: `xox[baprs]-[A-Za-z0-9-]{10,}`
- Stripe klíče: `sk_live_[A-Za-z0-9]{10,}`, `pk_live_[A-Za-z0-9]{10,}`
- Private keys: `-----BEGIN[A-Z ]*PRIVATE KEY-----`
- DB URLs s hesly: `(?:postgresql|mysql|mongodb)://[^:]+:[^@]+@[^\s]+`

**Fix:** Doplnit do `_LOG_REDACT_RE` v Pythonu i do wrapperu (viz konkrétní vzory v security review sekci).

Status: 🔲 Open

---

### H7. Log rotation race condition (TOCTOU) — `src/copilot_mcp_server.py:326-336`

**Severity:** High
**Category:** Security — symlink attack
**Model:** Claude Opus 4.5

Útočník může symlinknout `.log.1` na citlivý soubor. Při rotaci logu `log_path.replace(log_path.with_suffix(".log.1"))` přepíše symlink — tedy i cílový citlivý soubor — log daty.

**Fix:** Zkontrolovat symlinky před rotací:

```python
with _log_lock:
    if log_path.is_symlink():
        return  # odmítnout zápis do symlinku
    backup = log_path.with_suffix(".log.1")
    if log_path.exists() and log_path.stat().st_size > _LOG_MAX_BYTES:
        if backup.is_symlink():
            backup.unlink()
        log_path.replace(backup)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")
```

Status: 🔲 Open

---

## Medium Priority

### M6. Unicode bypass rejected_keywords — `src/copilot_mcp_server.py:339-344`

**Severity:** Medium
**Category:** Security — policy bypass
**Model:** Claude Opus 4.5

Fullwidth znaky (`ｒｍ -rf`), zero-width spaces (`se​curity`) nebo homoglyfy (Cyrillic `е` místo Latin `e`) obejdou keyword check.

**Fix:** Normalizovat vstup přes NFKC + odstranit zero-width znaky:

```python
import unicodedata

def _normalize(text: str) -> str:
    text = unicodedata.normalize('NFKC', text)
    return re.sub(r'[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]', '', text).lower()

def classify_task(task: str, rejected_keywords: list[str]) -> str:
    t = _normalize(task.strip())
    for kw in rejected_keywords:
        if _normalize(str(kw)) in t:
            return "reject_complex"
    return "allow"
```

Status: 🔲 Open

---

### M7. Semaphore DoS — 3× max timeout = dlouhá blokace — `src/copilot_mcp_server.py:323`

**Severity:** Medium
**Category:** Availability
**Model:** Claude Opus 4.5

Útočník odešle 3 tasky s timeoutem 600 s → ostatní requesty čekají až 10 minut.

**Fix:** Přidat timeout na samotné `semaphore.acquire()`:

```python
try:
    await asyncio.wait_for(_global_semaphore.acquire(), timeout=30)
except asyncio.TimeoutError:
    return "ERROR: Server busy, please retry later"
```

Status: 🔲 Open

---

### M8. Interní cesta úniky v error messages — `src/copilot_mcp_server.py:361`

**Severity:** Medium
**Category:** Information disclosure
**Model:** Claude Opus 4.5

```python
return f"ERROR: Wrapper not found: {WRAPPER}"  # odhaluje plnou cestu
```

**Fix:**

```python
log(f"Wrapper not found: {WRAPPER}")
return "ERROR: Internal configuration error. Contact administrator."
```

Status: 🔲 Open

---

### M9. Prázdný profil name po sanitizaci — `src/copilot_mcp_server.py:132-143`

**Severity:** Medium
**Category:** Robustness
**Model:** GPT-5.2

Profil s prázdným nebo jen speciálními znaky (např. `"!!!"`) se sanitizuje na `"___"` nebo `""`, což může vést k nevalidnímu MCP tool name.

**Fix:** Po sanitizaci validovat neprázdnost a shodu s `/^[a-zA-Z_][a-zA-Z0-9_]*$/`:

```python
def sanitize_profile_name(name: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if sanitized and sanitized[0].isdigit():
        sanitized = "_" + sanitized
    if not sanitized or not re.match(r'^[a-zA-Z_]\w*$', sanitized):
        raise ValueError(f"Profile name '{name}' produces invalid identifier '{sanitized}'")
    if sanitized != name:
        print(f"[WARNING] Profile name '{name}' sanitized to '{sanitized}'", file=sys.stderr)
    return sanitized
```

Status: 🔲 Open

---

## Low Priority

### L5. `make_handler()` subprocess lifecycle není testován — `tests/test_mcp_server.py`

**Severity:** Low
**Category:** Test coverage
**Model:** GPT-5.2

Scénáře bez testu:
- wrapper chybí / není executable
- wrapper vrátí non-zero exit code
- timeout branch (SIGTERM → SIGKILL)
- cancellation behavior
- semaphore se vždy uvolní (i při výjimkách)

**Fix:** Přidat `tests/test_handler.py` s `pytest-asyncio` a mock wrapper skriptem.

Status: 🔲 Open

---

### L6. Strukturované audit logy chybí — celý projekt

**Severity:** Low
**Category:** Observability / compliance
**Model:** Claude Sonnet 4.5

Logy jsou ad-hoc řetězce. Chybí audit trail: kdo požadoval co, které profily byly použity, co bylo blokováno, míra úspěšnosti.

**Fix:** Zavést `structlog` s JSON výstupem:

```python
import structlog
logger = structlog.get_logger()
logger.info("request_completed", profile=profile_name, duration_ms=elapsed*1000, output_len=len(out))
```

Status: 🔲 Open

---

### L7. Bash wrapper v security-critical path — architektonický dluh

**Severity:** Low (architektonické doporučení)
**Category:** Maintainability / testability
**Model:** Claude Sonnet 4.5

Shell skripty jsou hůře testovatelné, auditovatelné a náchylnější k injection než Python. Validační logika je rozdělena mezi dva jazyky.

**Doporučení (ADR-001):** Nahradit `copilot_wrapper.sh` pure Python modulem (`src/execution/copilot_runner.py`) — přímý `asyncio.create_subprocess_exec` bez mezivrstvy bash skriptu.

Status: ⚠️ Accepted/Known (backlog)

---

## Summary 2026-04-06

| Severity | Count | Klíčové oblasti |
|----------|-------|-----------------|
| Critical | 2 | `COPILOT_INSTALL_DIR` config bypass, `CancelledError` process leak |
| High | 4 | `load_config` neprovádí merge s defaults, nebezpečný `allowed_tools` fallback, neúplná redakce secrets, log rotation symlink attack |
| Medium | 4 | Unicode bypass, semaphore DoS, path leakage v errors, prázdný sanitized name |
| Low | 3 | Chybí handler testy, strukturované logy, bash wrapper jako architektonický dluh |

---

## Review 2026-04-06 (2. session — /review)

Reviewed files: `src/copilot_mcp_server.py`, `config.yaml`

Models: Claude Opus 4.5 (security), GPT-4.1 (code quality)

**Status legend:** 🔲 Open | ✅ Fixed | ⚠️ Accepted/Known

---

### Uzavřené položky z předchozích reviewů

Následující nálezy označené jako Open jsou v aktuálním kódu **již opraveny**:

| ID | Popis | Stav |
|----|-------|------|
| C1 | `COPILOT_INSTALL_DIR` config bypass | ✅ Fixed — `_validate_install_dir()` implementována |
| C2 | `CancelledError` → orphaned subprocesy | ✅ Fixed — explicitní `except asyncio.CancelledError` větev přidána |
| M6 | Unicode bypass v `rejected_keywords` | ✅ Fixed — `_normalize_text()` s NFKC normalizací implementována |
| M7 | Semaphore DoS bez timeoutu | ✅ Fixed — `asyncio.wait_for(acquire(), timeout=30)` přidán |
| M8 | Path leakage v error messages | ✅ Fixed — wrapper path se neexponuje klientovi |
| M9 | Prázdný sanitized profile name | ✅ Fixed — `sys.exit(1)` při nevalidním identifikátoru |
| H4 | `load_config` neprovádí merge s defaults | ✅ Fixed — `merged.update(valid_profiles)` implementováno |
| H7 | Log rotation symlink attack | ✅ Fixed — symlink check před rotací i zápisem |

---

### Nové nálezy

#### N-M1. `blocked_patterns` v shell wrapperu nepoužívají Unicode normalizaci — `src/copilot_wrapper.sh`

**Severity:** Medium
**Category:** Security — policy bypass
**Model:** Claude Opus 4.5

Python server normalizuje `rejected_keywords` přes `_normalize_text()` (NFKC + zero-width removal). Shell wrapper ale normalizuje pouze whitespace (`tr -s '[:space:]'`), bez Unicode normalizace pro `blocked_patterns`. Útočník může obejít pattern `rm -rf` použitím fullwidth znaků `ｒｍ -rf` (U+FF52, U+FF4D).

```bash
# wrapper — žádná NFKC normalizace:
NORMALIZED_LOWER="$(printf '%s' "$NORMALIZED_TASK" | tr '[:upper:]' '[:lower:]')"
for pattern in "${BLOCK_PATTERNS[@]}"; do
    if printf '%s' "$NORMALIZED_LOWER" | grep -qF -e "$pattern_lower"; then ...
```

**Poznámka:** `blocked_patterns` jsou defense-in-depth, ne primární security boundary. Hlavní ochrana je `--available-tools` v Copilot CLI. Riziko je reálné ale snížené.

**Fix:** Přidat Unicode normalizaci v shell wrapperu přes Python:

```bash
NORMALIZED_TASK="$(printf '%s' "$TASK" | python3 -c "
import sys, unicodedata, re
t = sys.stdin.read()
t = unicodedata.normalize('NFKC', t)
t = re.sub(r'[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]', '', t)
print(t, end='')
")"
```

Status: 🔲 Open

---

#### N-L1. Redakce nezachytí novější OpenAI `sk-proj-` klíče — `src/copilot_mcp_server.py:143`

**Severity:** Low
**Category:** Secret leakage
**Model:** Claude Opus 4.5

Vzor `sk-[A-Za-z0-9]{10,}` nezachytí nový formát OpenAI klíčů `sk-proj-...`:

```python
# Chybí:
>>> import re
>>> re.search(r"sk-[A-Za-z0-9]{10,}", "sk-proj-abc123def456")
None  # sk-proj- obsahuje pomlčku navíc
```

**Fix:**

```python
# změnit:
r"|sk-[A-Za-z0-9]{10,}"
# na:
r"|sk-(?:proj-)?[A-Za-z0-9]{10,}"
```

Totéž aplikovat v inline Python bloku `copilot_wrapper.sh`.

Status: 🔲 Open

---

#### N-L2. `COPILOT_BIN` env var není validován jako `COPILOT_INSTALL_DIR` — `src/copilot_wrapper.sh`

**Severity:** Low
**Category:** Security — defense-in-depth
**Model:** Claude Opus 4.5

`COPILOT_INSTALL_DIR` prochází přísnou validací proti trusted prefixům. `COPILOT_BIN` (override pro cestu ke Copilot CLI) žádnou takovou validaci nemá. Útočník s přístupem k prostředí procesu může nastavit `COPILOT_BIN=/tmp/evil` a nechat wrapper spustit libovolný binárník.

**Mitigace:** MCP server předává wrapperu vlastní environment a neexponuje `COPILOT_BIN` uživatelům. Riziko je nízké.

**Fix:** Přidat validaci v wrapperu analogicky k `_validate_install_dir`, nebo dokumentovat jako known limitation:

```bash
if [[ -n "$COPILOT_BIN" ]]; then
    resolved="$(realpath "$COPILOT_BIN" 2>/dev/null || true)"
    if [[ "$resolved" != "$HOME"* && "$resolved" != /usr/local/* && "$resolved" != /usr/* && "$resolved" != /opt/* ]]; then
        echo "[ERROR] COPILOT_BIN points outside trusted locations: $COPILOT_BIN" >&2
        exit 1
    fi
fi
```

Status: 🔲 Open

---

#### N-L3. GPT-4.1 falešný nález: semaphore underflow — `src/copilot_mcp_server.py:472-559`

**Severity:** N/A — NOT a bug
**Model:** GPT-4.1 (zamítnut)

GPT-4.1 nahlásil, že `finally: _global_semaphore.release()` se vykoná i když `acquire()` selhal timeoutem. Analýzou kódu bylo zjištěno, že toto **není chyba**: timeout v `acquire()` způsobí `return "ERROR: ..."` před vstupem do druhého `try` bloku, takže `finally` se nikdy nevykoná v tomto případě.

```python
try:
    await asyncio.wait_for(_global_semaphore.acquire(), timeout=30)
except asyncio.TimeoutError:
    return "ERROR: Server busy..."  # ← early return, nikdy nedosáhne finally

try:          # ← sem se dostaneme POUZE pokud acquire() uspělo
    ...
finally:
    _global_semaphore.release()  # ← vždy se spáruje s úspěšným acquire()
```

Status: ⚠️ Zamítnuto — kód je správný

---

### Summary 2026-04-06 (2. session)

| Severity | Count | Klíčové oblasti |
|----------|-------|-----------------|
| Medium | 1 | Unicode normalizace `blocked_patterns` v shell wrapperu |
| Low | 2 | `sk-proj-` chybí v redakci, `COPILOT_BIN` bez validace |
| Uzavřeno | 8 | C1, C2, M6, M7, M8, M9, H4, H7 — vše opraveno v aktuálním kódu |

---

## Review 2026-04-06 (3. session — /review)

**Modely:** Claude Opus 4.5 (security), GPT-4.1 (quality), Claude Sonnet 4.5 (architecture — výsledky čekají)
**Nové soubory od minulé session:** `src/redact.py` (sdílený modul pro redakci tajemství)
**Testy:** 93 / 93 ✅

### Uzavřená zjištění (z minulých sessions)

Nic nového od 2. session — N-M1, N-L1, N-L2 stále `Open`.

---

### Nová zjištění — 3. session

#### N-M2. `redact.py _main()` čte libovolný soubor bez validace cesty — `src/redact.py:86-98`

**Severity:** Medium
**Category:** Defense-in-depth / arbitrary file read
**Model:** Claude Opus 4.5

Funkce `_main()` přijme cestu k souboru jako `sys.argv[1]` a otevře ho bez jakékoli validace. Wrapper sám volá skript s kontrolovanou cestou `$TMP_OUT`, ale pokud by byl skript spuštěn jinak, může přečíst libovolný soubor přístupný procesu (včetně symlinků).

```python
# Aktuální kód — bez validace
path = sys.argv[1]
with open(path, encoding="utf-8", errors="replace") as fh:
    content = fh.read()
```

**Fix:**

```python
path = sys.argv[1]
if os.path.islink(path):
    print("ERROR: symlinks not allowed for input file", file=sys.stderr)
    sys.exit(1)
# Případně ověřit, že cesta leží v tmpdir:
# import tempfile; resolved = os.path.realpath(path)
# if not resolved.startswith(tempfile.gettempdir()):
#     sys.exit(1)
```

Status: 🔲 Open

---

#### N-M3. `redact_match()` vrátí `sk-REDACTED` pro `sk-proj-…` klíče — `src/redact.py:48-69`

**Severity:** Medium
**Category:** Secret leakage (partial prefix leak in logs)
**Model:** Claude Opus 4.5

`sk-proj-…` klíče (nový formát OpenAI) jsou zachyceny vzorem `sk-[A-Za-z0-9]{10,}` (pokud byl opraven dle N-L1), ale `redact_match()` vrátí `sk-REDACTED` místo `sk-proj-REDACTED`. V logu tak uniká informace, že jde o project key.

```python
# Přidat před obecnou sk- větev:
if lower.startswith("sk-proj-"):
    return "sk-proj-REDACTED"
```

Status: 🔲 Open

---

#### N-M4. `re.DOTALL` v `REDACT_PATTERN` může způsobit false-positive přes více řádků — `src/redact.py`

**Severity:** Medium
**Category:** Redakce — false positives / over-redaction
**Model:** GPT-4.1

Příznak `re.DOTALL` způsobí, že `.` matchuje i newline. Vzory jako `Bearer\s+[A-Za-z0-9._~+/=-]{10,}` nebo `(?:api[_-]?key|token|secret|password)=...` pak mohou pohltit více řádků a nadměrně redakovat výstup.

Totéž platí pro PEM/CERTIFICATE pattern — je greedy a s DOTALL může pohltit celý blok i s libovolným obsahem mezi `BEGIN` a `END`.

**Fix:** Odebrat `re.DOTALL`, pokud neexistuje konkrétní test case, který ho vyžaduje. Pokud jsou víceřádkové tajemství záměrné, přidat explicitní testy.

```python
# Změnit:
REDACT_PATTERN = re.compile(r"...", re.IGNORECASE | re.DOTALL)
# Na:
REDACT_PATTERN = re.compile(r"...", re.IGNORECASE)
```

Status: 🔲 Open

---

#### N-M5. Testovací mezery — chybí scénáře pro víceřádkové/malformované vstupy — `tests/test_mcp_server.py`

**Severity:** Medium
**Category:** Test coverage
**Model:** GPT-4.1 + Claude Opus 4.5

Chybí testy pro:
- Tajemství rozdělené přes více řádků (pokud je DOTALL záměrný)
- Malformované vstupy (`key==value`, `key=val=extra`)
- Homogl yfy mimo aktuální mapu (Cyrillic у, В; Greek Α, Β)
- PEM blok s extra whitespace nebo zalomením

Status: 🔲 Open

---

#### N-L4. `_HOMOGLYPH_MAP` postrádá běžné lookalike znaky — `src/copilot_mcp_server.py:427-443`

**Severity:** Low
**Category:** Security hardening — homoglyph bypass
**Model:** Claude Opus 4.5 + GPT-4.1 (oba nezávisle nahlásili)

Aktuální mapa nepokrývá:

| Chybějící | Unicode | Podobá se |
|-----------|---------|-----------|
| Cyrillic у | U+0443 | Latin y |
| Cyrillic В | U+0412 | Latin B |
| Cyrillic ү | U+04AF | Latin y |
| Greek Α | U+0391 | Latin A |
| Greek Β | U+0392 | Latin B |

Příklad bypassu: `securitу` (Cyrillic у na konci) se nenormalizuje na `security`.

**Fix:**

```python
_HOMOGLYPH_MAP = str.maketrans({
    # ... existující záznamy ...
    "\u0443": "y",   # у → y
    "\u0412": "b",   # В → B
    "\u04AF": "y",   # ү → y
    "\u0391": "a",   # Α → A
    "\u0392": "b",   # Β → B
})
```

Status: 🔲 Open

---

#### N-L5. `redact_match()` split na `=` — edge case `key=val=extra` — `src/redact.py`

**Severity:** Low
**Category:** Correctness edge case
**Model:** GPT-4.1

Kód správně používá `s.split("=", 1)`, takže `key=val=extra` se rozdělí na `["key", "val=extra"]` — hodnota bude celá redakována. Toto je žádoucí chování, ale není pokryto testem.

**Fix:** Přidat test:

```python
def test_redact_key_value_with_extra_equals():
    result = redact("password=abc=def123456")
    assert "abc=def123456" not in result
```

Status: 🔲 Open

---

#### N-L6. Dynamický import `redact.py` tiše potlačí syntax error v modulu — `src/copilot_mcp_server.py:147-162`

**Severity:** Low
**Category:** Error surfacing / diagnostics
**Model:** GPT-4.1

`exec_module()` není obaleno try/except, takže syntax error nebo import error v `redact.py` se propaguje jako nespecifická chyba a může být těžko diagnostikovatelný.

**Fix:**

```python
try:
    loader.exec_module(mod)
except Exception as exc:
    raise ImportError(f"Failed to load redact module from {_rpath}: {exc}") from exc
```

Status: 🔲 Open

---

#### N-L7. Chybí test: `blocked_patterns` se samými prázdnými řetězci — `tests/test_mcp_server.py`

**Severity:** Low
**Category:** Test coverage
**Model:** Claude Opus 4.5

Kód správně stripuje prázdné řetězce z `blocked_patterns`, ale není pokryto testem pro případ, kdy jsou **všechny** položky prázdné.

**Fix:**

```python
def test_coerce_blocked_patterns_all_empty_strings(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    blocked_patterns:\n      - ''\n      - ''\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"]["blocked_patterns"] == []
```

Status: 🔲 Open

---

#### N-L8. TOCTOU race v dynamickém importu `redact.py` — `src/copilot_mcp_server.py:147-162`

**Severity:** Low
**Category:** Race condition (accept risk)
**Model:** Claude Opus 4.5

Mezi `if _rpath.exists()` a `exec_module()` teoreticky může dojít k záměně souboru. Riziko je minimální, protože cesta je relativní ke skriptu serveru, nikoli uživatelem ovládaná.

**Mitigace:** Přijato riziko — útočník s přístupem k install dir může modifikovat i samotný server.

Status: ⚠️ Přijato riziko

---

#### N-L9. Truncation v `_main()` je podle počtu znaků, ne bytů — `src/redact.py`

**Severity:** Low
**Category:** Documentation / correctness
**Model:** GPT-4.1

`redacted[:max_chars]` zkracuje podle počtu znaků (Unicode code points). Pro ASCII výstup je toto ekvivalentní bytům, ale pro non-ASCII obsah může byte-oriented consumer dostat jiný počet bytů.

**Fix:** Zdokumentovat v docstringu nebo v README, že `max_chars` je Unicode character count. Pokud wrapper předpokládá byty, změnit logiku na `redacted.encode("utf-8")[:max_chars].decode("utf-8", errors="ignore")`.

Status: 🔲 Open (dokumentace)

---

### Výsledky architektury (Claude Sonnet 4.5)

Celkové hodnocení: **7.5/10** — architektura je vhodná, 93 unit testů, ale chybí integrační testy a pár kritických oblastí.

#### A-H1. Wrapper používá `/tmp` pro dočasné soubory — `src/copilot_wrapper.sh:244-245`

**Severity:** High
**Category:** Security — temp file location
**Model:** Claude Sonnet 4.5

Wrapper vytváří temp soubory v globálním `/tmp`, přestože projekt zakázuje `/tmp` a má vlastní `INSTALL_DIR`. Symlink attack (TOCTOU) na `/tmp` je klasický útok.

**Fix:** Nahradit `/tmp` za `$INSTALL_DIR/tmp/` nebo použít `mktemp -p "$INSTALL_DIR"`.

Status: 🔲 Open

---

#### A-H2. Žádné integrační testy — celý stack

**Severity:** High
**Category:** Test coverage — integration
**Model:** Claude Sonnet 4.5

Všech 93 testů jsou unit testy. Chybí:
- End-to-end: server → wrapper → Copilot CLI
- Concurrent requests (semaphore chování pod zátěží)
- Client disconnect cleanup

Status: 🔲 Open

---

#### A-H3. `_register_tools()` bez testů — `src/copilot_mcp_server.py`

**Severity:** High
**Category:** Test coverage — kritická startup funkce
**Model:** Claude Sonnet 4.5

Funkce `_register_tools()` zahrnuje detekci kolizí názvů a `sys.exit(1)` při chybě, ale má 0 % test coverage. Při regresu v konfiguraci může server tiše selhat při startu.

Status: 🔲 Open

---

#### A-H4. Selhání dynamického importu `redact.py` není otestováno — `src/copilot_mcp_server.py:147-162`

**Severity:** High
**Category:** Test coverage — resilience
**Model:** Claude Sonnet 4.5

Pokud `redact.py` chybí nebo je poškozený, server spadne. Tento scénář není pokryt testy. (Překrývá se s N-L6.)

**Fix:** Přidat testy pro: chybějící soubor, syntax error v modulu, nesprávná verze rozhraní.

Status: 🔲 Open

---

#### A-M1. Log rotace je naivní a má netestované edge cases — `src/copilot_mcp_server.py`

**Severity:** Medium
**Category:** Robustness
**Model:** Claude Sonnet 4.5

Rotace logů selže pokud `.log.1` je adresář; chybí limit počtu rotací; edge cases nejsou testovány.

Status: 🔲 Open

---

#### A-M2. Žádná observabilita — metriky, health check, stav semaforu

**Severity:** Medium
**Category:** Operability
**Model:** Claude Sonnet 4.5

Server nemá žádné metriky, health check endpoint, ani způsob jak zjistit stav semaforu při diagnostice "Server busy" chyb.

Status: 🔲 Open (known limitation)

---

#### A-M3. Profile merging: uživatel může oslabit security profil

**Severity:** Medium
**Category:** Security policy
**Model:** Claude Sonnet 4.5

Uživatel může přepsat `allowed_tools` v security profilu a přidat bash — tím obchází záměr profilu. Chybí hierarchie "read-only" profilů nebo explicitní zamknutí.

Status: 🔲 Open

---

#### A-M4. `_redact` testy ověřují jen jeden typ tokenu vs. wrapper — `tests/test_mcp_server.py`

**Severity:** Medium
**Category:** Test coverage — cross-component
**Model:** Claude Sonnet 4.5

Cross-check mezi serverem a wrapperem pokrývá pouze jeden typ tokenu. Ostatní typy tajemství (GitHub PAT, AWS key, Bearer token atd.) nejsou porovnávány mezi oběma vrstvami.

Status: 🔲 Open

---

### Summary 2026-04-06 (3. session)

Detailní architekturní analýza uložena v: **`ARCHITECTURE_REVIEW.md`** (28 kB)

| Severity | Count | Klíčové oblasti |
|----------|-------|-----------------|
| High (arch) | 4 | tmp soubory, chybějící integrační/startup testy |
| Medium (arch) | 4 | Log rotace, observabilita, profile merging, cross-check |
| Medium (security/quality) | 4 | DOTALL false positives, arbitrary file read, sk-proj- leak, test gaps |
| Low | 6 | Homoglyph map, edge cases, error surfacing, doc |
| Přijato riziko | 1 | TOCTOU v importlib |

---

## Review 2026-04-06 (4. session — /review)

**Modely:** Claude Opus 4.5 (security), GPT-4.1 (quality), Claude Sonnet 4.6 (architecture)
**Testy:** 113 / 113 ✅ (+20 od 3. session)
**Opraveno od 3. session:** A-H1 (tmp soubory → INSTALL_DIR/tmp), A-H4 částečně (_load_redact_module testováno), A-M4 (cross-check pro všechny typy tokenů)

---

### Nová zjištění — 4. session

#### A4-H1. `_SEMAPHORE_TIMEOUT` crashne server při nevalidním env var — `src/copilot_mcp_server.py:447`

**Severity:** High
**Category:** Robustness — startup crash
**Model:** Claude Sonnet 4.6

Nechráněné `int(...)` na module level crashne server při startu pokud operátor nastaví `COPILOT_SEMAPHORE_TIMEOUT=abc` nebo `1.5`. Každý jiný env-to-int převod v kódu je ošetřen; tento není.

```python
# Aktuální kód — crashne na ValueError:
_SEMAPHORE_TIMEOUT: int = max(1, int(os.environ.get("COPILOT_SEMAPHORE_TIMEOUT", "30")))
```

**Fix:**

```python
try:
    _SEMAPHORE_TIMEOUT = max(1, int(os.environ.get("COPILOT_SEMAPHORE_TIMEOUT", "30")))
except (TypeError, ValueError):
    print("[WARNING] COPILOT_SEMAPHORE_TIMEOUT invalid, using 30", file=sys.stderr)
    _SEMAPHORE_TIMEOUT = 30
```

**Test:** `test_semaphore_timeout_invalid_env_falls_back_to_30` — monkeypatch `"abc"`, reload modulu, assert `_SEMAPHORE_TIMEOUT == 30`.

Status: 🔲 Open

---

#### A4-H2. `await proc.wait()` bez timeoutu po SIGKILL — `src/copilot_mcp_server.py:600, 638`

**Severity:** High
**Category:** Robustness — potential infinite hang
**Model:** Claude Sonnet 4.6

Po SIGKILL kód dělá holé `await proc.wait()` bez timeoutu. Linux zombie procesy nebo kernel bug mohou zdržet `waitpid`. V `CancelledError` větvi je toto obzvlášť nebezpečné: zaseknutý `await` drží async task naživu navzdory cancel, semaphore slot unikne (finally release nikdy neproběhne).

```python
# Problematické místo (časem 600 a 638):
os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
...
await proc.wait()  # ← žádný timeout
```

**Fix:** Konzistentně obalit všechny tři cleanup větve:

```python
try:
    await asyncio.wait_for(proc.wait(), timeout=3)
except asyncio.TimeoutError:
    pass  # SIGKILL sent, waitpid stuck — move on
```

Status: 🔲 Open

---

#### A4-M1. Triplovaný cleanup kód s odchylkami — `src/copilot_mcp_server.py:587–640`

**Severity:** Medium
**Category:** Maintainability — code duplication
**Model:** Claude Sonnet 4.6

SIGTERM → `wait(5)` → SIGKILL → `wait()` je napsán třikrát v `except TimeoutError`, `except Exception`, `except CancelledError` s jemnými ale věcnými rozdíly (Exception větev má extra `try/except` wrapper; CancelledError a TimeoutError mají holé `await proc.wait()`). Jakákoliv oprava musí být aplikována na tři místa.

**Fix:** Extrahovat helper funkci:

```python
async def _kill_proc(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            pass
```

Status: 🔲 Open

---

#### A4-M2. `load_config()` vrací dvě různé shapes — `src/copilot_mcp_server.py:356–425`

**Severity:** Medium
**Category:** Design — inconsistent return value
**Model:** Claude Sonnet 4.6

Pokud jsou nalezeny validní profily, funkce vrátí celý YAML dict (může obsahovat libovolné top-level klíče). Fallback vrátí vždy `{"profiles": {...}}`. Callers se brání `config.get("profiles", {})`, ale shapes nejsou garantovány — budoucí klíč mohl být tiše přebit uživatelským YAMLem.

**Fix:**

```python
# Místo:  return data
return {"profiles": _merge_profiles_with_defaults(valid_profiles)}
```

Status: 🔲 Open

---

#### A4-M3. `except asyncio.CancelledError` za `except Exception` — maintenance trap — `src/copilot_mcp_server.py:603, 623`

**Severity:** Medium
**Category:** Correctness risk / maintainability
**Model:** Claude Sonnet 4.6 + Claude Opus 4.5

V Python 3.8+ je `CancelledError` podtřídou `BaseException` (ne `Exception`), takže pořadí je technicky funkční. Nicméně:
- Porušuje universální konvenci (specifické before obecné)
- Každý vývojář neznalý změny hierarchie to přečte jako dead code a "opraví" smazáním
- Budoucí refaktor na `except BaseException` nebo Python <3.8 kompatibilitu tuto větev tiše pohltí

**Fix:** Přesunout `except asyncio.CancelledError` PŘED `except Exception`.

Status: 🔲 Open

---

#### A4-M4. Chybí délkový limit na `model` a `prompt_prefix` — `src/copilot_mcp_server.py:319–330`

**Severity:** Medium
**Category:** Security / input validation
**Model:** Claude Sonnet 4.6 + Claude Opus 4.5

`_coerce_profile_fields` ověřuje typ (musí být string), ale ne délku. Extrémně dlouhý `prompt_prefix` může:
1. Přesáhnout Linux `ARG_MAX` (~2 MB) → `OSError` za runtime, ne při načtení configu
2. Fakticky obejít `max_input_length` — task je omezen, ale `len(prefix) + len(task)` se nevaliduje
3. Model hodnota s `--` nebo `-` prefixem může Copilot CLI splést s flagy

**Fix:**

```python
_MAX_MODEL_LEN = 128
_MAX_PREFIX_LEN = 4096

# v _coerce_profile_fields:
if field == "model" and isinstance(p.get("model"), str):
    if p["model"].startswith("-") or len(p["model"]) > _MAX_MODEL_LEN:
        p["model"] = default  # + warning
if field == "prompt_prefix" and isinstance(p.get("prompt_prefix"), str):
    if len(p["prompt_prefix"]) > _MAX_PREFIX_LEN:
        p["prompt_prefix"] = p["prompt_prefix"][:_MAX_PREFIX_LEN]  # truncate + warning
```

Status: 🔲 Open

---

#### A4-T1. `make_handler` kritické chybové větve bez testů — `tests/test_mcp_server.py`

**Severity:** Medium
**Category:** Test coverage — regression risk
**Model:** Claude Sonnet 4.6

Tři handlery nemají coverage:

| Větev | Trigger | Riziko |
|-------|---------|--------|
| `except asyncio.CancelledError` | Klient disconnect | Process leak + semaphore leak |
| `except Exception` (spawn failure) | WRAPPER není executable | Tiché selhání |
| Semaphore timeout (`Server busy`) | 3 paralelní long-running | Pokryto env testem, ne přes make_handler |

CancelledError je nejvyšší riziko: pokud dojde k nechtěnému přesunutí `finally`, disconnect klienta trvale pohltí semaphore slot.

**Fix:** Přidat testy (unit-testovatelné teď bez integrace):

```python
async def test_make_handler_cancelled_kills_process_and_reraises():
    # Mock create_subprocess_exec → proc.communicate() raises CancelledError
    # Assert: os.killpg called, CancelledError propagates, semaphore released

async def test_make_handler_spawn_failure_returns_error():
    # Mock create_subprocess_exec raises OSError
    # Assert: returns "ERROR: Failed to call wrapper", semaphore released

async def test_make_handler_semaphore_timeout_returns_busy():
    # Exhaust semaphore (acquire 3x), call handler
    # Assert: returns "ERROR: Server busy..."
```

Status: 🔲 Open

---

#### Q4-M1. Regex pro key-value vzory postrádá word boundary — `src/redact.py`

**Severity:** Medium
**Category:** Redakce — false negatives / missed secrets
**Model:** GPT-4.1

Vzor `(?:api[_-]?key|token|secret|password|passwd)=...` nemá `\b` word boundary. Subřetězec `api_key=secret` v `my_api_key=secret` je zachycen, ale název proměnné `my_api_key` v logu odhalí prefix.

**Fix:**

```python
r"(?<![a-zA-Z0-9_])(?:api[_-]?key|token|secret|password|passwd)="
```

(negative lookbehind namísto `\b` aby fungovalo přes `re.DOTALL`)

Status: 🔲 Open

---

#### Q4-L1. Homoglyfová mapa aplikována před lowercase — `src/copilot_mcp_server.py:509–512`

**Severity:** Low
**Category:** Security hardening — bypass gap
**Model:** GPT-4.1

Aktuální pořadí v `_normalize_text`: NFKC → zero-width strip → **homoglyfová mapa** → lower(). Mapa obsahuje pouze lowercase Cyrillic (`\u0430`=а, `\u0435`=е, …). Uppercase Cyrillic А (U+0410) projde přes mapu nenormalizován, pak `lower()` jej sice převede na а (U+0430), ale překlad už nenastane. Výsledek: `Аrchitecture` (s uppercase Cyrillic А) nebude zablokováno.

**Fix:** Přesunout lower() PŘED homoglyfovou mapu:

```python
def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]", "", text)
    text = text.lower()              # ← nejdříve lowercase
    return text.translate(_HOMOGLYPH_MAP)  # ← pak mapa (pracuje s lowercase Cyrillic)
```

Status: 🔲 Open

---

#### S4-L1. INSTALL_DIR s null bytem nebo newline není odmítnut — `src/copilot_mcp_server.py:25–48`

**Severity:** Low
**Category:** Defense-in-depth — path validation
**Model:** Claude Opus 4.5

`_validate_install_dir()` ověřuje trusted prefix ale neodmítá `\x00`, `\n`, `\r` v cestě. Null byte způsobí truncaci cesty na některých syscalls; newline by mohla ovlivnit log parsing.

**Fix:**

```python
if any(c in str(resolved) for c in "\x00\n\r"):
    print(f"[ERROR] COPILOT_INSTALL_DIR contains invalid characters", file=sys.stderr)
    sys.exit(1)
```

Status: 🔲 Open

---

#### A4-T2. Chybí test pro `COPILOT_SEMAPHORE_TIMEOUT` s nevalidní hodnotou — `tests/test_mcp_server.py`

**Severity:** Low
**Category:** Test coverage — regression
**Model:** Claude Sonnet 4.6

Po implementaci A4-H1 fix je nutné přidat test, jinak může guard být omylem odstraněn.

**Fix:**

```python
def test_semaphore_timeout_invalid_env_falls_back_to_30(monkeypatch):
    monkeypatch.setenv("COPILOT_SEMAPHORE_TIMEOUT", "not_a_number")
    import importlib, copilot_mcp_server as srv
    importlib.reload(srv)
    assert srv._SEMAPHORE_TIMEOUT == 30
```

Status: 🔲 Open (blokováno A4-H1)

---

#### A4-L1. `_resolve_allowed_tools` dostane sanitizované jméno, DEFAULT_CONFIG má originální — `src/copilot_mcp_server.py:680`

**Severity:** Low
**Category:** Design — edge case silent failure
**Model:** Claude Sonnet 4.6

Pokud název profilu vyžaduje sanitizaci (př. `"simple "` → `"simple_"`), handler dostane `"simple_"`. `_resolve_allowed_tools("simple_", ...)` pak nenajde `DEFAULT_CONFIG["profiles"]["simple_"]` a vrátí `[]` (disable all tools) místo built-in defaultu. Toto je safe failover (most restrictive), ale chová se překvapivě.

**Fix:** Předat původní `_name` do `make_handler` pro použití v `_resolve_allowed_tools`, nebo zdokumentovat chování.

Status: 🔲 Open

---

#### Q4-L2. `log()` neomezuje délku zprávy — `src/copilot_mcp_server.py:450–468`

**Severity:** Low
**Category:** Robustness
**Model:** GPT-4.1

Velmi dlouhá zpráva (např. chyba ze subprocess) může předčasně spustit rotaci nebo nafouknout log soubor. Handler truncuje stderr na 200 znaků, ale `log()` samotný nemá limit.

**Fix:** Truncovat zprávy v `log()`:

```python
def log(msg: str) -> None:
    if len(msg) > 10_000:
        msg = msg[:10_000] + "...[truncated]"
    ...
```

Status: 🔲 Open

---

### Summary 2026-04-06 (4. session)

| Severity | Count | Klíčové oblasti |
|----------|-------|-----------------|
| High | 2 | SEMAPHORE_TIMEOUT crash, proc.wait() bez timeoutu |
| Medium | 7 | Cleanup duplikace, load_config() shape, CancelledError pořadí, délkové limity, test gaps, regex word boundary |
| Low | 5 | Homoglyph pořadí, null chars v INSTALL_DIR, test pro bad env, _resolve_allowed_tools, log truncation |

**Top 3 priority:**
1. **A4-H1** — jednořádkový fix + test, zabrání crash při startu
2. **A4-H2** — timeout po SIGKILL, zabrání semaphore leak při CancelledError
3. **A4-M3** — přesunout CancelledError před Exception (předejde maintenance pádu)

---

### Fixes applied 2026-04-06 (post-review)

Applied fixes in src/copilot_mcp_server.py:
- Guarded COPILOT_SEMAPHORE_TIMEOUT parsing (fallback=30) — prevents startup crash (A4-H1)
- Centralized subprocess cleanup: added async _kill_proc(proc) and replaced inline cleanup (A4-M1)
- Bounded wait on proc.wait() after SIGKILL to avoid hangs (A4-H2)
- Reordered except asyncio.CancelledError before generic Exception and used _kill_proc in handlers (A4-M3)

All tests passed: 113/113.

Status: A4-H1, A4-H2, A4-M1, A4-M3 — ✅ Fixed

---

## Review 2026-04-06 (5. session — /review)

Modely: Claude Opus 4.5 (security), gpt-5-mini (quality, architecture)
Testy: 113/113 ✅

Nová zjištění — 5. session

Architektura (R5-A1..R5-A9):

- R5-A1: Hardcoded concurrency
  - Popis: _DEFAULT_MAX_CONCURRENCY je pevně nastaveno (3). Chybí COPILOT_MAX_CONCURRENCY a per-profile omezení; semafor je vytvořen při importu.
  - Doporučení: Přidat validovaný env var COPILOT_MAX_CONCURRENCY, umožnit per-profile max_concurrency a přesunout tvorbu semaforu do fáze startupu nebo lazy-init.
  - Stav: OPEN

- R5-A2: Chybějící metriky/observability pro backpressure
  - Popis: Není metrika pro obsazenost semaforu, časouts a počet odmítnutí.
  - Doporučení: Přidat počitadla/gauges (semaphore_acquired, semaphore_wait_time, semaphore_timeouts) a jednoduchý MCP health/metrics tool.
  - Stav: OPEN

- R5-A3: Graceful shutdown
  - Popis: Není spolehlivé zachycení SIGTERM/SIGINT pro zrušení běžících úloh a korektní _kill_proc s grace period.
  - Doporučení: Přidat signal handlers, orchestrace cancelací, a použít centralizované _kill_proc s časovými limity.
  - Stav: OPEN

- R5-A4: Import-time side-effects a startup hardening
  - Popis: Některé validace a resource alokace běží při importu, což zhoršuje testovatelnost a start-up resilienci.
  - Doporučení: Přesunout validace a init do explicitní startup fáze; přidat self-check endpoint/test.
  - Stav: OPEN

- R5-A5: Profile merge/normalizace
  - Popis: Merging profile keys není konzistentní (case, None vs [] semantics) — zamětnutí může vést k neočekávaným povolením/zakázáním nástrojů.
  - Doporučení: Normalizovat/validovat profile keys při načtení a jasně zdokumentovat difference mezi None a []
  - Stav: OPEN

- R5-A6: Log rotation a symlink guard tests
  - Popis: No tests for log rotation, symlinked log dirs, nebo bounded log growth.
  - Doporučení: Přidat unit/integration testy a monitorování rotace; falback chování zdokumentovat.
  - Stav: OPEN

- R5-A7: Integration tests for _kill_proc and startup ordering
  - Popis: Chybí integrační testy, které ověřují escalation (wait -> SIGTERM -> SIGKILL) a pořadí inicializace.
  - Doporučení: Přidat tests/test_kill_proc.py a e2e startup smoke tests.
  - Stav: OPEN

- R5-A8: redact.py resolution/order mismatch
  - Popis: Server a wrapper používají odlišné import/search pořadí pro redact.py, což může vést k divergujícím chováním.
  - Doporučení: Standardizovat vyhledávání modulu a dokumentovat očekávané pořadí; preferovat explicitní resolve v instalaci.
  - Stav: OPEN

- R5-A9: Chybějící runtime health/metrics tool
  - Popis: Neexistuje jednoduchý MCP tool, který by vrátil health/metrics JSON.
  - Doporučení: Přidat malý MCP tool pro health/metrics, publikovat základní metriky.
  - Stav: OPEN

Bezpečnost (R5-S1..R5-S3):

- R5-S1: Bounds on profile-supplied argv/strings
  - Popis: Profilem dodané listy/řetězce (blocked_patterns, allowed_tools, prompt_prefix, argv) nejsou ohraničeny → risk ARG_MAX, OOB allocation, long inputs.
  - Doporučení: Zavést max délky a počty v _coerce_profile_fields a make_handler; pokud přesaženo, truncate+warn nebo reject podle politky; ošetřit OSError při spawn.
  - Stav: OPEN

- R5-S2: PATH ordering / binary resolution hardening
  - Popis: Reliance na PATH pořadí může vést k spuštění nečekaných binárek z uživatelských složek.
  - Doporučení: Preferovat system-level directories nebo resolve kritické nástroje na absolutní cesty; přidat kontrolu realpath a whitelist pokud nutné.
  - Stav: OPEN

- R5-S3: Venv/python selection validation
  - Popis: Volba python interpreteru z venv není důkladně validována.
  - Doporučení: Validate realpath(venv_python) je pod TRUSTED_PREFIXES (INSTALL_DIR, /usr/bin); fallback na system python; tests.
  - Stav: OPEN

Kvalita (R5-Q1..R5-Q4):

- R5-Q1: Homoglyph pipeline pořadí
  - Popis: Homoglyph translate se provádí před lower() → uppercase homoglyphs mohou uniknout.
  - Doporučení: Spustit text.lower() před translate nebo obohatit mapu; přidat test pro uppercase homoglyph.
  - Stav: OPEN

- R5-Q2: Unused imports / parse clarity
  - Popis: Některé importy (např. shlex) nejsou použity; parsing semantics nejsou pokryty testy.
  - Doporučení: Odebrat unused imports a přidat clarity tests/comments.
  - Stav: OPEN

- R5-Q3: Wrapper kill invocation safety
  - Popis: shell wrapper by mohl volat kill s nestrukturovanými PIDy.
  - Doporučení: Použít robustní pattern (kill -- "$pid" v loopu) nebo explicitní PID handling; přidat smoke test.
  - Stav: OPEN

- R5-Q4: _REQUIRED_EXPORTS tidy
  - Popis: Některé export/const definice nejsou používány.
  - Doporučení: Odstranit nebo korektně použít; přidat testy pro expected exports.
  - Stav: OPEN

Souhrn: Architektura 9 zjištění, Bezpečnost 3, Kvalita 4 — všechny označeny jako OPEN. Doporučení: aplikovat R5-S1..S3 + R5-A1..A3 jako priorita.

---
