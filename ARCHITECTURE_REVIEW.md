# Architecture & Test Coverage Review
**MCP Copilot Delegate Server — Full Codebase Review**

**Review Date:** 2025-01-29  
**Codebase State:** Post-fixes (93 Python tests + 27 bash tests, all passing)  
**LOC:** Server: 648, Redact: 110, Wrapper: 336, Tests: 1,312

---

## Executive Summary

**Overall Assessment: GOOD** — Architecture is appropriate for the use case with some areas for improvement.

**Strengths:**
- Clean separation of concerns (config → server → wrapper → Copilot CLI)
- Comprehensive input validation (NFKC, homoglyph, zero-width, keyword rejection)
- Defense in depth (server-side + wrapper-side validation)
- Excellent test coverage for known bugs and edge cases
- Single source of truth for redaction patterns (redact.py)

**Concerns:**
- Some test coverage gaps (see findings below)
- Dynamic import pattern has hidden dependencies
- Limited observability (no metrics, basic logging)
- No integration/end-to-end tests
- Wrapper uses `/tmp` despite global prohibition

---

## Specific Findings

### HIGH Severity Issues

#### H1: Wrapper Uses `/tmp` Despite Global Prohibition
**Location:** `src/copilot_wrapper.sh:244-245`  
**Issue:** Wrapper creates temp files using `mktemp -t copilot_XXXXXX`, which writes to `/tmp` on Linux. The codebase instructions explicitly prohibit /tmp usage: "NEVER write to /tmp, /var/tmp, or use mktemp — these paths are forbidden."

**Code:**
```bash
TMP_OUT="$(mktemp -t copilot_XXXXXX)"
TMP_ERR="$(mktemp -t copilot_XXXXXX)"
```

**Recommendation:**
- Create temp files in `INSTALL_DIR/tmp/` or use `-p` flag: `mktemp -p "$INSTALL_DIR/tmp" copilot_XXXXXX`
- Add cleanup on server shutdown to remove temp directory
- Update tests to verify temp files are not in /tmp

#### H2: No Integration Tests
**Location:** Entire codebase  
**Issue:** All tests are unit tests. There are NO integration tests that verify:
- Server → Wrapper → Copilot CLI full flow
- MCP protocol communication (stdio transport)
- Config loading → tool registration → tool invocation chain
- Semaphore behavior under concurrent load
- Process cleanup on client disconnect (asyncio.CancelledError path)

**Recommendation:**
- Add at least 3 integration tests:
  1. Full flow test with fake Copilot binary (happy path)
  2. Concurrent requests test (verify semaphore limits work)
  3. Client disconnect test (verify subprocess cleanup via CancelledError)

#### H3: `_register_tools()` Not Tested
**Location:** `src/copilot_mcp_server.py:621-639`  
**Issue:** The tool registration function has complex logic (collision detection, sanitization, mcp.tool() decoration) but is never called in tests. Test coverage for this critical startup path is 0%.

**Current code:**
```python
def _register_tools() -> None:
    config = load_config()
    seen: set[str] = set()
    for _name, _profile in config.get("profiles", {}).items():
        sanitized = sanitize_profile_name(_name)
        if sanitized in seen:
            print(f"[ERROR] Profile name collision...", file=sys.stderr)
            sys.exit(1)  # <-- THIS EXIT PATH IS UNTESTED
        seen.add(sanitized)
        mcp.tool()(make_handler(sanitized, _profile))
```

**Recommendation:**
- Add test: `test_register_tools_collision_exits()` — verify sys.exit(1) on duplicate sanitized names
- Add test: `test_register_tools_creates_all_tools()` — verify mcp.tool() called for each profile
- Add test: `test_register_tools_with_custom_profile()` — verify custom profiles are registered

#### H4: Redact Module Dynamic Import Fragility
**Location:** `src/copilot_mcp_server.py:146-162`  
**Issue:** The dynamic import of `redact.py` uses a for-else loop with `raise ImportError` in the else clause. If redact.py is not found, the server crashes at import time with a clear error — but this failure mode is NOT tested.

**Code:**
```python
for _rpath in (...):
    if _rpath.exists():
        # ... load module
        break
else:
    raise ImportError("Could not find redact.py...")
```

**Missing tests:**
1. What happens if redact.py is missing?
2. What happens if redact.py exists but has a syntax error?
3. What happens if redact.py is missing REDACT_PATTERN or redact_match?

**Recommendation:**
- Add test: `test_server_import_fails_if_redact_missing()` — monkeypatch Path.exists to always return False, assert ImportError
- Add test: `test_server_import_fails_if_redact_malformed()` — create a redact.py with invalid syntax, assert import fails
- Consider: Add version check or signature validation to redact module

---

### MEDIUM Severity Issues

#### M1: Log Rotation is Naive
**Location:** `src/copilot_mcp_server.py:387-418`  
**Issue:** Log rotation logic has edge cases:
- Rotation happens when file > 10MB, but no limit on number of rotations (.log.1, .log.2, ...)
- If .log.1 is a symlink, it's silently unlinked (good), but if it's a directory or unreadable file, replace() may fail with OSError
- All OSError exceptions are silently swallowed — a failing log write never raises an error

**Code:**
```python
if log_path.exists() and log_path.stat().st_size > _LOG_MAX_BYTES:
    backup = log_path.with_suffix(".log.1")
    if backup.is_symlink():
        backup.unlink()
    log_path.replace(backup)  # <-- Can fail if backup is directory
```

**Recommendation:**
- Add test: `test_log_rotation_when_backup_is_directory()` — verify behavior
- Add test: `test_log_rotation_when_backup_is_unwritable()` — verify no crash
- Consider: Limit to .log.1 only (delete old .log.1 before rotate)
- Consider: Add log level to log() function for filtering

#### M2: Semaphore Timeout Not Configurable
**Location:** `src/copilot_mcp_server.py:396-397, 528`  
**Issue:** The semaphore timeout is hardcoded to 30 seconds:
```python
await asyncio.wait_for(_global_semaphore.acquire(), timeout=30)
```
This is not configurable per-profile or via environment variable. A profile with `timeout: 600` (10 min) may have requests queue for 30s even when slots are available.

**Recommendation:**
- Make semaphore timeout configurable: `COPILOT_SEMAPHORE_TIMEOUT` env var with default 30
- Document the relationship between profile timeout and semaphore timeout
- Add test: verify semaphore timeout respects env var

#### M3: No Metrics or Observability
**Location:** Entire codebase  
**Issue:** The server logs to a file, but provides:
- No request count metrics
- No latency/duration metrics
- No error rate tracking
- No way to query current semaphore state (how many slots in use?)
- No health check endpoint

**Recommendation:**
- Add simple metrics: request_count, error_count, total_duration (logged every N requests)
- Add health check: MCP tool `health_check` that returns semaphore state, last request time
- Consider: Prometheus metrics or structured logging (JSON)

#### M4: Profile Merging is Confusing
**Location:** `src/copilot_mcp_server.py:376-382`  
**Issue:** Config merging logic starts with DEFAULT_CONFIG profiles, then overlays user profiles. This means:
- User cannot DELETE default profiles (simple, security, code_review always present)
- User CAN override default profiles (potentially dangerous if they weaken security profile)

**Code:**
```python
merged: dict = dict(DEFAULT_CONFIG["profiles"])
merged.update(valid_profiles)  # User profiles override defaults
```

**Current behavior:**
- config.yaml with `profiles: {custom: {...}}` → gets simple + security + code_review + custom
- config.yaml with `profiles: {security: {bash in allowed_tools}}` → OVERRIDES read-only security!

**Recommendation:**
- Add test: `test_user_cannot_weaken_security_profile()` — verify security profile cannot add bash
- Consider: Make default profiles immutable (user can only add NEW profiles, not override)
- Document: merging behavior clearly in README

#### M5: Wrapper `pick_python_bin()` Uses Untrusted PATH
**Location:** `src/copilot_wrapper.sh:110-122`  
**Issue:** `pick_python_bin()` first checks venv, then uses `command -v python3` which searches PATH. The PATH is extended at line 105 to include `~/.local/bin`, but an attacker could still influence PATH before the wrapper runs.

**Code:**
```bash
export PATH="${HOME}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:${PATH:-/usr/bin:/bin}"
# ...
python_bin="$(command -v python3 2>/dev/null || true)"
```

**Risk:** If a malicious actor sets PATH before wrapper invocation, they could inject a fake python3.

**Recommendation:**
- Use absolute paths for critical binaries: `/usr/bin/python3` or `/usr/local/bin/python3`
- Add test: verify wrapper does not execute python3 from attacker-controlled PATH
- Consider: Require VENV_PYTHON to be set explicitly

#### M6: Empty `blocked_patterns` or `rejected_keywords` Silently Accepted
**Location:** `src/copilot_mcp_server.py:269-278`  
**Issue:** `_coerce_profile_fields()` removes empty strings from lists:
```python
if empty_count:
    print(f"[WARNING] ... has {empty_count} empty string(s), removing them", ...)
    p[field] = [item for item in p[field] if item]
```

But if ALL items are empty, the list becomes `[]`, which is semantically "no restrictions." For `blocked_patterns: ['']`, the user likely made a mistake, but the server silently allows everything.

**Recommendation:**
- Add test: `test_blocked_patterns_all_empty_logs_warning()`
- Consider: If list becomes empty after removing empties, log a CRITICAL warning

#### M7: Redact Function Name Collision Risk
**Location:** `src/copilot_mcp_server.py:165-171`  
**Issue:** The server defines `_redact()` which calls `_LOG_REDACT_RE.sub(_redact_match_fn, text)`. The names `_redact`, `_redact_match_fn`, `_LOG_REDACT_RE` are module-level globals set by the dynamic import loop. If redact.py is refactored and renames `redact_match` → `redact_func`, the server breaks.

**Code:**
```python
_redact_match_fn = _rmod.redact_match  # Assumes redact.py exports 'redact_match'
```

**Recommendation:**
- Add test: `test_redact_module_has_required_exports()` — assert hasattr(redact_module, 'REDACT_PATTERN')
- Add test: `test_redact_module_has_redact_match()` — assert callable
- Consider: Use getattr with fallback or explicit version check

#### M8: Test Imports `_redact` Directly
**Location:** `tests/test_mcp_server.py:13`  
**Issue:** Test imports `_redact` from copilot_mcp_server:
```python
from copilot_mcp_server import ... _redact, ...
```

But `_redact` is a function that uses the dynamically imported `_LOG_REDACT_RE` and `_redact_match_fn`. If the dynamic import fails or points to a different redact.py than expected, the test may pass but the production code may fail.

**Question from user:** "Is this test reliable?"

**Answer:** **Partially reliable.** The test DOES verify that server._redact() produces correct output, but it does NOT verify:
1. That redact.py is actually being used (vs. a different pattern)
2. That server._redact() and wrapper redact.py produce IDENTICAL output (the L1 test does this, but only for one token type)

**Recommendation:**
- Add test: `test_redact_server_and_wrapper_identical_for_all_token_types()` — iterate over all token types (ghp_, sk-, Bearer, etc.) and verify server._redact() matches wrapper redact.py output
- This would be a true cross-check test

---

### LOW Severity Issues

#### L1: No Test for Config File YAML Parsing Errors
**Location:** `src/copilot_mcp_server.py:313-319`  
**Issue:** `load_config()` catches `yaml.YAMLError` but no test verifies this branch:
```python
except (OSError, yaml.YAMLError) as exc:
    print(f"[WARNING] Could not load config {CONFIG_FILE}: {exc}", file=sys.stderr)
    return DEFAULT_CONFIG
```

**Recommendation:**
- Add test: `test_load_config_malformed_yaml_falls_back()` — write invalid YAML (e.g., `key: [unclosed`), verify DEFAULT_CONFIG returned

#### L2: Wrapper Exit Codes Not Documented
**Location:** `src/copilot_wrapper.sh` (entire file)  
**Issue:** Wrapper uses exit codes 1-7 with specific meanings, but there's no central documentation. Tests know the codes, but users don't.

**Current codes:**
- 1: Missing/empty task
- 2: Task too long
- 3: Control characters
- 4: Dangerous pattern
- 5: Copilot execution failed
- 6: Copilot binary not found
- 7: Python not found for redaction

**Recommendation:**
- Add comment block at top of wrapper documenting exit codes
- Add to README

#### L3: `sanitize_profile_name()` Edge Case: Unicode
**Location:** `src/copilot_mcp_server.py:174-194`  
**Issue:** `sanitize_profile_name()` uses regex `[^a-zA-Z0-9_]` which replaces non-ASCII chars. A profile name like `security-日本語` becomes `security_____` (valid) but may surprise users.

**Recommendation:**
- Add test: `test_sanitize_profile_name_unicode_becomes_underscores()`
- Document: profile names should be ASCII-only

#### L4: No Test for `_decode()` Helper
**Location:** `src/copilot_mcp_server.py:473-475`  
**Issue:** `_decode()` is a small helper but has no dedicated test:
```python
def _decode(b: bytes | None) -> str:
    return b.decode("utf-8", errors="replace") if b else ""
```

**Recommendation:**
- Add test: `test_decode_none_returns_empty_string()`
- Add test: `test_decode_invalid_utf8_uses_replacement_char()`

#### L5: Config File Path Selection Not Tested
**Location:** `src/copilot_mcp_server.py:58-68`  
**Issue:** CONFIG_FILE resolution logic (repo config.yaml vs. INSTALL_DIR config.yaml) is never tested. The logic is:
1. If `<repo>/config.yaml` exists → use it
2. Else use `INSTALL_DIR/config.yaml`

**Recommendation:**
- Add test: `test_config_file_prefers_repo_over_install_dir()`
- Add test: `test_config_file_falls_back_to_install_dir_when_repo_missing()`

#### L6: Log File Creation Race Condition (Theoretical)
**Location:** `src/copilot_mcp_server.py:73-76`  
**Issue:** Log directory creation happens at module import time, but OSError is silently ignored:
```python
try:
    Path(os.path.join(INSTALL_DIR, "logs")).mkdir(parents=True, exist_ok=True)
except OSError:
    pass  # log() itself is also guarded
```

If INSTALL_DIR is unwritable, logs/ is never created, and all subsequent log() calls silently fail. This is by design (never let logging break requests), but makes debugging difficult.

**Recommendation:**
- Add startup check: log a test message at server start, verify it's written
- Add to README: how to check if logging is working

---

## Architecture Questions (from User)

### Q1: Is the architecture appropriate for the use case?

**Answer: YES**, with minor concerns.

**Strengths:**
- **Clean layering:** Config → MCP Server → Wrapper → Copilot CLI is a good separation
- **Defense in depth:** Input validation at both server (classify_task) and wrapper (blocked_patterns, control chars) layers
- **Config-driven:** Adding a new profile is just YAML editing + restart
- **Appropriate tech stack:** FastMCP for MCP protocol, bash wrapper for subprocess isolation, YAML for config

**Concerns:**
- **Over-reliance on subprocess:** Every request spawns a shell + Copilot process. This is fine for low-volume use but doesn't scale to high concurrency.
- **No caching:** Identical requests recompute. Consider caching for idempotent tasks.
- **Tight coupling to wrapper script:** If wrapper is not installed correctly, server fails. Could use fallback or bundled wrapper.

### Q2: Are there missing test scenarios?

**Answer: YES.** See HIGH findings H2 (integration tests), H3 (_register_tools), H4 (redact import failure), and MEDIUM M1 (log rotation edge cases).

**Additional missing scenarios:**
1. **Concurrent requests exceeding semaphore limit** — verify queuing works
2. **Client disconnect during long-running task** — verify subprocess cleanup
3. **Wrapper killed by OOM killer** — verify server doesn't hang
4. **Malformed redact.py** — verify server fails fast at startup
5. **Config with 100 profiles** — verify performance
6. **Profile name with only emoji** — verify sanitization
7. **Task with 5000 chars of only spaces** — verify strip() before length check

### Q3: Is dynamic import of redact.py a good design? Alternatives?

**Answer: ACCEPTABLE**, but fragile.

**Pros:**
- Single source of truth for redaction patterns
- Wrapper and server never drift apart
- Easy to extend (add new token type once in redact.py)

**Cons:**
- Hidden dependency (server breaks if redact.py is missing/malformed)
- No version checking (redact.py could change interface)
- Import happens at module level (fails before mcp.run())
- Test reliability concern (M8)

**Alternatives:**

**Option A: Package redact as a proper Python package**
```python
from copilot_agent.redact import REDACT_PATTERN, redact_match
```
- Pro: Normal import semantics, better error messages
- Pro: Can version check: `if redact.__version__ < "1.0": raise`
- Con: Requires packaging redact.py as installable module

**Option B: Inline redaction in server + wrapper duplicates pattern**
- Pro: No dynamic import magic
- Con: BREAKS single source of truth (patterns can drift)
- **Verdict: BAD**

**Option C: Load redact.py as a resource file (not executable module)**
```python
import json
patterns = json.loads(Path(__file__).parent / "redact_patterns.json")
```
- Pro: Data-driven, easy to extend
- Con: Loses the redact_match() logic (need to reimplement in both places)

**Recommendation: Keep current design**, but:
1. Add startup validation: check redact.py exports REDACT_PATTERN and redact_match
2. Add version check: redact.py should export `__version__ = "1.0"`
3. Add test for import failure (H4)

### Q4: Is there redundancy or dead code?

**Answer: NO significant dead code**, but some duplication:

**Duplication:**
1. **Validation logic duplicated server + wrapper:**
   - Server: classify_task() checks rejected_keywords
   - Wrapper: blocked_patterns check (line 200-209)
   
   These serve DIFFERENT purposes (server=policy, wrapper=safety), so duplication is acceptable.

2. **Timeout logic duplicated:**
   - Profile timeout (config)
   - Semaphore acquire timeout (30s hardcoded)
   - Wrapper timeout (profile timeout + 5s grace)
   
   These are intentionally layered, not redundant.

3. **Path resolution duplicated:**
   - CONFIG_FILE resolution (lines 58-68)
   - redact.py search (lines 147-162)
   - wrapper REDACT_SCRIPT search (wrapper lines 321-334)
   
   **Recommendation:** Extract to a shared `_find_file(candidates)` helper.

**Unused code:**
- `_LOG_MAX_BYTES` and log rotation: Used, but edge cases not tested (M1)
- All imports are used

### Q5: Any observability/debugging gaps?

**Answer: YES**, significant gaps (see M3).

**Current observability:**
- ✅ Logs to file (timestamped, redacted)
- ✅ Logs profile name, success/fail, length
- ✅ Logs stderr warnings from wrapper
- ❌ No metrics (request count, latency, error rate)
- ❌ No health check endpoint
- ❌ No way to query semaphore state
- ❌ No request tracing (correlation IDs)
- ❌ No log levels (everything is INFO)

**Debugging difficulty:**
- **Semaphore timeout:** "Server busy" error — no way to tell how many requests are queued or how long they've been waiting
- **Wrapper failures:** Only logs first 200 chars of stderr — may truncate useful info
- **Config errors:** Warnings go to stderr, easy to miss in production

**Recommendations:**
1. Add MCP tool `server_status()` → returns {semaphore_slots_available, last_request_time, error_count}
2. Add log levels: `log(msg, level="INFO")` with filtering
3. Add request ID to all log lines for tracing
4. Consider structured logging (JSON) for machine parsing

### Q6: `redact.py` has both module API and CLI mode. Is this good design?

**Answer: YES**, this is a good pattern.

**Module API:**
```python
from redact import REDACT_PATTERN, redact_match, redact
```
Used by: server (via dynamic import)

**CLI mode:**
```bash
python3 redact.py <input-file> <max-output-chars>
```
Used by: wrapper (line 336)

**Pros:**
- Single file, dual interface (library + CLI)
- CLI mode is tested indirectly via wrapper tests
- No dependency on server for wrapper usage

**Cons:**
- CLI mode has no --help or usage docs (only error message on wrong args)
- CLI mode does not validate max-output-chars > 0 until runtime (line 89)

**Recommendation:**
- Add `--help` flag to CLI mode
- Add argparse for better CLI experience
- Add test: `test_redact_cli_help_flag()`
- Add test: `test_redact_cli_invalid_max_output_exits()`

**Alternative design: Separate CLI and module**
```
src/
  redact_lib.py   # Module API
  redact_cli.py   # CLI that imports redact_lib
```
- Pro: Clearer separation
- Con: Wrapper must now find TWO files (redact_lib.py + redact_cli.py)
- **Verdict: Not worth the complexity**

### Q7: Module-level globals populated by loop-with-else — implications?

**Location:** `src/copilot_mcp_server.py:146-162`

**Code:**
```python
for _rpath in (...):
    if _rpath.exists():
        ...
        _LOG_REDACT_RE = _rmod.REDACT_PATTERN
        _redact_match_fn = _rmod.redact_match
        break
else:
    raise ImportError(...)
```

**Implications:**

**Positive:**
- Runs at import time → fails fast if redact.py is missing
- Module-level globals are initialized before any request
- break-else pattern is idiomatic for "search with fallback"

**Negative:**
- **Hard to test:** Can't easily mock Path.exists() for import-time code
- **Hard to reason about:** _LOG_REDACT_RE is set by side effect of loop, not explicit assignment
- **Fragile:** If loop logic changes (e.g., add third search path), must update else clause
- **No type hints:** _LOG_REDACT_RE and _redact_match_fn have no type annotations (mypy doesn't know they're re.Pattern and Callable)

**Type safety issue:**
```python
_LOG_REDACT_RE: re.Pattern  # Type annotation AFTER the loop would help
```

**Recommendations:**
1. Add type annotations AFTER the loop (or use typing.cast)
2. Add test for import failure (H4)
3. Consider refactoring to a function:
   ```python
   def _load_redact_module():
       for rpath in (...):
           if rpath.exists():
               return load_module(rpath)
       raise ImportError(...)
   
   _redact_mod = _load_redact_module()
   _LOG_REDACT_RE = _redact_mod.REDACT_PATTERN
   ```
   This makes the code easier to test and reason about.

### Q8: Test imports `_redact` — is this reliable?

**Location:** `tests/test_mcp_server.py:13`

**Answer: PARTIALLY RELIABLE** (see M8 above).

The test verifies:
- ✅ Server `_redact()` produces correct output for tested token types
- ✅ Server `_redact()` uses the dynamically imported module

The test does NOT verify:
- ❌ Server `_redact()` and wrapper redact.py produce IDENTICAL output for ALL token types
- ❌ Dynamic import succeeds (assumes it does)
- ❌ redact.py has not been tampered with

**Current L1 test (`test_server_redact_uses_shared_redact_module`) DOES cross-check:**
```python
token = "ghp_" + "A" * 20
assert srv._redact(f"token: {token}") == mod.redact(f"token: {token}")
```

But only for ONE token type (ghp_). 

**Recommendation:**
Add comprehensive cross-check test:
```python
def test_server_and_wrapper_redact_identical_for_all_tokens():
    """L1 extended: server and wrapper produce identical output for ALL token types."""
    import copilot_mcp_server as srv
    import redact  # Direct import
    
    test_cases = [
        "ghp_AAAAAAAAAAAAAAAAAAAA",
        "sk-AAAAAAAAAAAAAAAAAAAAAA",
        "Bearer AAAAAAAAAAAAAAAAAAAAAA",
        "github_pat_AAAAAAAAAAAAAA",
        # ... all token types
    ]
    
    for token in test_cases:
        server_output = srv._redact(f"token: {token}")
        wrapper_output = redact.redact(f"token: {token}")
        assert server_output == wrapper_output, f"Mismatch for {token}"
```

---

## Test Coverage Summary

**Well-Covered Areas:**
- ✅ classify_task (11 tests: basic, case, unicode, homoglyphs)
- ✅ load_config (18 tests: shape validation, coercion, merging)
- ✅ _coerce_profile_fields (15 tests: int coercion, list validation, comma rejection)
- ✅ sanitize_profile_name (8 tests: hyphens, digits, special chars, exit on empty)
- ✅ _redact (18 tests: all token types, case-insensitive, multiple secrets)
- ✅ _normalize_text (5 tests: NFKC, zero-width, homoglyphs)
- ✅ make_handler (8 tests: empty task, rejected keyword, wrapper errors, semaphore timeout)

**Under-Covered Areas:**
- ❌ _register_tools (0 tests)
- ❌ main() (0 tests)
- ❌ Integration tests (0 tests)
- ❌ Log rotation edge cases (0 tests)
- ❌ Dynamic import failure (0 tests)
- ❌ _decode() helper (0 tests)
- ❌ Config file path selection (0 tests)
- ❌ Malformed YAML (0 tests)

**Wrapper Coverage (27 bash tests):**
- ✅ Exit codes 1-4 (missing task, too long, control chars, blocked pattern)
- ✅ Secret redaction (sk-, Bearer, github_pat_, Slack, PostgreSQL URLs)
- ✅ Unicode truncation
- ✅ --allowed-tool flags
- ✅ Function-call RHS not redacted
- ❌ Exit code 6 (Copilot not found) — only tested indirectly
- ❌ Exit code 7 (Python not found) — not tested
- ❌ Wrapper timeout (124) — not tested

---

## Actionable Recommendations (Prioritized)

### Must Fix (Before Production)

1. **H1: Replace `/tmp` usage** — Create temp files in `INSTALL_DIR/tmp/`
2. **H2: Add integration tests** — At minimum: happy path, concurrency, client disconnect
3. **H3: Test `_register_tools()`** — Verify collision detection, sys.exit(1) path

### Should Fix (Next Sprint)

4. **H4: Test redact.py import failure** — Verify server fails fast if redact.py is missing/malformed
5. **M1: Test log rotation edge cases** — Backup is directory, unwritable, etc.
6. **M2: Make semaphore timeout configurable** — Add `COPILOT_SEMAPHORE_TIMEOUT` env var
7. **M3: Add basic observability** — server_status() MCP tool, log levels
8. **M4: Document profile merging** — Clarify that user can override default profiles

### Nice to Have (Technical Debt)

9. **M7: Add redact.py version check** — `if redact.__version__ != "1.0": raise`
10. **M8: Add comprehensive L1 cross-check** — Verify server/wrapper identical for all tokens
11. **L1: Test malformed YAML** — Verify fallback to DEFAULT_CONFIG
12. **L2: Document wrapper exit codes** — Add comment block at top of wrapper
13. **L5: Test config file path selection** — Repo vs. INSTALL_DIR priority

---

## Conclusion

**Overall Quality: GOOD (7.5/10)**

The codebase is well-structured with excellent test coverage for known bugs and edge cases. The architecture is appropriate for the use case. Main concerns are:
- Lack of integration tests (all tests are unit tests)
- Limited observability (no metrics, basic logging)
- Wrapper uses `/tmp` (prohibited by instructions)
- Some untested edge cases (log rotation, dynamic import failure, _register_tools)

**Recommendation: APPROVED for continued development**, pending fixes for H1-H3.

---

## Appendix: Test Naming Patterns

**Observed Patterns:**
- `test_<function>_<scenario>_<expected_result>` — e.g., `test_classify_task_fullwidth_keyword_blocked`
- `test_<function>_<edge_case>` — e.g., `test_sanitize_profile_name_empty_string_exits`
- `test_<bug_number>_<description>` — e.g., `test_allowed_tools_missing_falls_back_to_default_config` (Bug 1)

**Strengths:**
- Descriptive names clearly state intent
- Bug numbers reference fix history
- Consistent naming makes test discovery easy

**Weakness:**
- Some tests are very long (`test_explicit_empty_allowed_tools_not_overridden_by_default`)
- No consistent prefix for integration vs. unit tests (all use `test_`)

**Recommendation:**
- Prefix integration tests with `test_integration_*` when added
- Keep unit test names as-is (already good)
