#!/usr/bin/env python3
"""MCP server that delegates tasks to GitHub Copilot CLI based on config profiles."""
import os
import re
import signal
import sys
import threading
import unicodedata
from collections.abc import AsyncIterable
from datetime import datetime
from pathlib import Path
from subprocess import PIPE

import anyio
import yaml
from mcp.server.fastmcp import FastMCP

try:
    import pwd
except ImportError:  # pragma: no cover - pwd is unavailable on Windows
    pwd = None  # type: ignore[assignment]

mcp = FastMCP("copilot-delegate")


def _uid_home() -> Path | None:
    """Return the current user's home from the passwd database when available.

    Claude may launch MCP subprocesses with a synthetic HOME during health checks.
    In that case Path.home() no longer points at the real per-user install root,
    so also trust the home directory associated with the current uid.
    """
    if pwd is None:
        return None
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except (KeyError, OSError, RuntimeError):
        return None


def _trusted_install_prefixes() -> list[Path]:
    """Return trusted parent directories for COPILOT_INSTALL_DIR validation."""
    trusted: list[Path] = []
    seen: set[str] = set()
    for candidate in (
        Path.home(),
        _uid_home(),
        Path("/usr/local/share"),
        Path("/usr/share"),
        Path("/opt"),
        Path("/var/lib"),
    ):
        if candidate is None:
            continue
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        trusted.append(resolved)
    return trusted

def _validate_install_dir(raw: str) -> str:
    """Return resolved path when it is inside a trusted prefix, else exit.

    Prevents loading wrapper binaries or config from attacker-controlled paths
    by requiring COPILOT_INSTALL_DIR to be under the user's home directory or
    common system-wide installation prefixes.
    """
    try:
        resolved = Path(raw).resolve()
    except (OSError, ValueError) as exc:
        print(f"[ERROR] COPILOT_INSTALL_DIR is not a valid path ({raw!r}): {exc}", file=sys.stderr)
        sys.exit(1)
    trusted = _trusted_install_prefixes()
    for prefix in trusted:
        try:
            resolved.relative_to(prefix)
            return str(resolved)
        except ValueError:
            continue
    print(
        f"[ERROR] COPILOT_INSTALL_DIR points outside trusted locations: {raw!r}\n"
        f"        Allowed prefixes: {[str(p) for p in trusted]}",
        file=sys.stderr,
    )
    sys.exit(1)


# INSTALL_DIR: env var > default home path (used for wrapper, logs, venv).
# When COPILOT_INSTALL_DIR is set, it is validated to prevent loading config
# or wrapper binaries from an attacker-controlled path.
_raw_install_dir = os.environ.get("COPILOT_INSTALL_DIR")
INSTALL_DIR = _validate_install_dir(_raw_install_dir) if _raw_install_dir \
    else str(Path.home() / ".local/share/ai-agent/copilot")

# CONFIG_FILE resolution — priority order (highest first):
#   1. <repo-root>/config.yaml  — present when the server runs directly from the
#      cloned repository (src/copilot_mcp_server.py → ../config.yaml).
#      This wins over INSTALL_DIR so that `git pull && python src/...` picks up
#      the repo's config without requiring a re-install.
#   2. INSTALL_DIR/config.yaml  — the standard installed location; edited by the
#      user after running install-copilot-agent.sh.
# Note: COPILOT_INSTALL_DIR env var overrides the INSTALL_DIR default (see above),
# so point 2 is also user-controllable without changing the source.
_script_config = Path(__file__).resolve().parent.parent / "config.yaml"
CONFIG_FILE = str(_script_config) if _script_config.exists() else os.path.join(INSTALL_DIR, "config.yaml")

WRAPPER = os.path.join(INSTALL_DIR, "bin/copilot_wrapper.sh")
LOG_FILE = os.path.join(INSTALL_DIR, "logs/copilot-mcp.log")

try:
    Path(os.path.join(INSTALL_DIR, "logs")).mkdir(parents=True, exist_ok=True)
except OSError:
    pass  # log() itself is also guarded; a bad INSTALL_DIR won't crash the server

DEFAULT_CONFIG = {
    "profiles": {
        "simple": {
            "model": "gpt-4o-mini",
            "description": (
                "Delegate simple developer tasks: code generation, boilerplate, "
                "small refactors, shell snippets, code explanations. "
                "Do NOT use for architecture, security, or auth decisions."
            ),
            "prompt_prefix": (
                "Respond concisely in the same language as the task. "
                "Return only code or a direct answer."
            ),
            "timeout": 300,
            "max_input_length": 5000,
            "max_output_length": 16000,
            "allowed_tools": ["view", "show_file", "create", "edit", "grep", "glob", "bash", "web_fetch"],
            "blocked_patterns": ["rm -rf", "mkfs", "dd if=", "shutdown", "reboot", "chmod 777"],
            "rejected_keywords": [
                "architecture", "security", "authentication", "authorization",
                "autorizace", "architektura", "bezpecnost", "compliance", "multi-tenant",
            ],
        },
        "security": {
            "model": "gpt-4o",
            "description": (
                "Analyze code or configurations for security vulnerabilities, "
                "threat modeling, or OWASP issues. Use for security-focused review."
            ),
            "prompt_prefix": (
                "You are a security expert. Analyze thoroughly and list specific "
                "vulnerabilities with severity."
            ),
            "timeout": 600,
            "max_input_length": 30000,
            "max_output_length": 15000,
            "allowed_tools": ["view", "show_file", "grep", "glob", "web_fetch"],
            "blocked_patterns": [],
            "rejected_keywords": [],
        },
        "code_review": {
            "model": "gpt-4o",
            "description": (
                "Supplement code review: check for code quality, naming, complexity, "
                "test coverage, and best practices. Not a replacement for human review."
            ),
            "prompt_prefix": (
                "Review this code for quality, readability, and best practices. "
                "Be specific and actionable."
            ),
            "timeout": 600,
            "max_input_length": 30000,
            "max_output_length": 15000,
            "allowed_tools": ["view", "show_file", "grep", "glob", "web_fetch"],
            "blocked_patterns": [],
            "rejected_keywords": [],
        },
    }
}


# ---------------------------------------------------------------------------
# Redaction — import from shared redact.py (single source of truth).
# This ensures the server log path and the wrapper output path always use
# the same patterns; no manual syncing required.  Search order:
#   1. Same directory as this file  (dev / src/ layout)
#   2. ../bin/ relative to this file (installed mcp/ → bin/ layout)
# ---------------------------------------------------------------------------
def _load_redact_module():
    """Locate and load redact.py, returning the module.

    Search order:
    1. Same directory as this file (dev / src/ layout)
    2. ../bin/ relative to this file (installed mcp/ → bin/ layout)

    Raises ImportError if not found or if required exports are missing.
    Refactored from loop-with-else to make the code testable.
    """
    import importlib.util as _ilu
    candidates = [
        Path(__file__).parent / "redact.py",
        Path(__file__).parent.parent / "bin" / "redact.py",
    ]
    for rpath in candidates:
        if rpath.exists():
            spec = _ilu.spec_from_file_location("redact", rpath)
            mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            # Validate that the loaded module exports the required symbols so
            # the server fails fast if redact.py was partially updated.
            _required = ("REDACT_PATTERN", "redact_match", "redact")
            missing = [sym for sym in _required if not hasattr(mod, sym)]
            if missing:
                raise ImportError(
                    f"redact.py at {rpath} is missing required exports: {missing}.  "
                    "Run install-copilot-agent.sh --update to restore it."
                )
            return mod
    raise ImportError(
        "Could not find redact.py.  Expected it next to copilot_mcp_server.py "
        f"or in {candidates[-1]}.  Run install-copilot-agent.sh --update."
    )


_rmod = _load_redact_module()
_LOG_REDACT_RE: re.Pattern = _rmod.REDACT_PATTERN
_redact_match_fn = _rmod.redact_match


def _redact(text: str) -> str:
    """Redact common secret patterns before writing to log.

    Uses the canonical pattern from redact.py (single source of truth shared
    with copilot_wrapper.sh) so the two consumers never drift apart.
    """
    return _LOG_REDACT_RE.sub(_redact_match_fn, text)


async def _kill_proc(proc) -> None:
    """Terminate a subprocess process group gracefully, then escalate with timeouts.

    Ensures consistent cleanup with bounded waits so the server cannot hang on
    waitpid/zombie cases. This centralises the logic across multiple exception
    handlers.
    """
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass
    else:  # pragma: no cover - Windows-only fallback
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        with anyio.fail_after(5):
            await proc.wait()
    except TimeoutError:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        else:  # pragma: no cover - Windows-only fallback
            try:
                proc.kill()
            except OSError:
                pass
        try:
            with anyio.fail_after(3):
                await proc.wait()
        except TimeoutError:
            # Best-effort: give up after the second timeout to avoid hanging.
            pass


async def _read_stream(stream: AsyncIterable[bytes] | None) -> bytes:
    """Read a subprocess byte stream fully without assuming an asyncio backend."""
    if stream is None:
        return b""
    data = bytearray()
    async for chunk in stream:
        data.extend(chunk)
    return bytes(data)


async def _communicate_process(proc) -> tuple[bytes, bytes]:
    """Collect stdout/stderr concurrently while waiting for process exit."""
    stdout = b""
    stderr = b""

    async def drain_stdout() -> None:
        nonlocal stdout
        stdout = await _read_stream(proc.stdout)

    async def drain_stderr() -> None:
        nonlocal stderr
        stderr = await _read_stream(proc.stderr)

    async with anyio.create_task_group() as tg:
        tg.start_soon(drain_stdout)
        tg.start_soon(drain_stderr)
        await proc.wait()

    return stdout, stderr



def sanitize_profile_name(name: str) -> str:
    """Convert a profile name to a valid Python/MCP tool identifier."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    # Identifiers must not start with a digit.
    if sanitized and sanitized[0].isdigit():
        sanitized = "_" + sanitized
    # A name made entirely of non-identifier chars (e.g. "!!!") produces an
    # empty or all-underscore string that is not a valid MCP tool name.
    if not sanitized or not re.match(r"^[a-zA-Z_]\w*$", sanitized):
        print(
            f"[ERROR] Profile name '{name}' produces an invalid MCP identifier '{sanitized}'. "
            f"Rename this profile to start with a letter or underscore.",
            file=sys.stderr,
        )
        sys.exit(1)
    if sanitized != name:
        print(
            f"[WARNING] Profile name '{name}' sanitized to '{sanitized}' for MCP compatibility",
            file=sys.stderr,
        )
    return sanitized


def _coerce_profile_fields(profile: dict, pname: str) -> dict:
    """Coerce and validate individual profile field types, returning a safe copy.

    Called at load time so handler closures always receive correctly-typed values.
    Mismatched fields are coerced where possible (e.g. "600" → 600) or replaced
    with safe defaults, with a stderr warning in both cases.
    """
    p = dict(profile)

    for field, default in (("timeout", 300), ("max_input_length", 5000), ("max_output_length", 16000)):
        val = p.get(field)
        if val is None:
            continue
        coerced = val
        if isinstance(val, bool):
            print(
                f"[WARNING] {CONFIG_FILE}: profile '{pname}': '{field}' must be an integer, "
                f"not bool ({val!r}), using default {default}",
                file=sys.stderr,
            )
            p[field] = default
            continue
        if not isinstance(val, int):
            try:
                coerced = int(val)
                print(
                    f"[WARNING] {CONFIG_FILE}: profile '{pname}': '{field}' coerced from "
                    f"{type(val).__name__} to int ({val!r} → {coerced})",
                    file=sys.stderr,
                )
            except (TypeError, ValueError):
                print(
                    f"[WARNING] {CONFIG_FILE}: profile '{pname}': '{field}' invalid ({val!r}), "
                    f"using default {default}",
                    file=sys.stderr,
                )
                p[field] = default
                continue
        if coerced <= 0:
            print(
                f"[WARNING] {CONFIG_FILE}: profile '{pname}': '{field}' must be > 0 "
                f"(got {coerced!r}), using default {default}",
                file=sys.stderr,
            )
            p[field] = default
            continue
        p[field] = coerced

    for field in ("blocked_patterns", "rejected_keywords", "allowed_tools"):
        val = p.get(field)
        if val is None:
            continue
        if not isinstance(val, list):
            print(
                f"[WARNING] {CONFIG_FILE}: profile '{pname}': '{field}' must be a list "
                f"(got {type(val).__name__}), ignoring",
                file=sys.stderr,
            )
            # For allowed_tools, use None rather than [] so that an invalid type
            # is distinguishable from an intentionally empty list.
            # None triggers the DEFAULT_CONFIG fallback for built-in profiles;
            # [] means "disable all tools" for this profile.
            p[field] = None if field == "allowed_tools" else []
            continue
        non_str = [item for item in val if not isinstance(item, str)]
        if non_str:
            print(
                f"[WARNING] {CONFIG_FILE}: profile '{pname}': '{field}' has non-string items "
                f"{non_str!r}, coercing to str",
                file=sys.stderr,
            )
            p[field] = [str(item) for item in val]
        # Empty strings in blocked_patterns/rejected_keywords would cause
        # grep -F -e "" to match every task (empty pattern matches everything).
        empty_count = sum(1 for item in p[field] if not isinstance(item, str) or not item)
        if empty_count:
            print(
                f"[WARNING] {CONFIG_FILE}: profile '{pname}': '{field}' has {empty_count} "
                f"empty string(s), removing them",
                file=sys.stderr,
            )
            p[field] = [item for item in p[field] if item]
        # allowed_tools items must not contain commas.  A single entry
        # like "view,bash" would be forwarded as-is to --available-tools=view,bash
        # by the wrapper, silently expanding the whitelist.  Reject such items.
        if field == "allowed_tools":
            comma_items = [t for t in p[field] if "," in t]
            if comma_items:
                print(
                    f"[WARNING] {CONFIG_FILE}: profile '{pname}': 'allowed_tools' contains "
                    f"items with commas {comma_items!r}.  Write each tool as a separate list "
                    f"entry (e.g. [view, bash]), not as a comma-separated string.  "
                    f"Removing offending items.",
                    file=sys.stderr,
                )
                p[field] = [t for t in p[field] if "," not in t]

    # String fields passed directly into subprocess argv — must be strings.
    # A YAML object like `model: {nested: value}` would survive shape validation
    # but cause a runtime failure when building the command list.
    for field, default in (("model", "gpt-4o-mini"), ("prompt_prefix", ""), ("description", "")):
        val = p.get(field)
        if val is None:
            continue
        if not isinstance(val, str):
            print(
                f"[WARNING] {CONFIG_FILE}: profile '{pname}': '{field}' must be a string "
                f"(got {type(val).__name__}), using default",
                file=sys.stderr,
            )
            p[field] = default

    return p


def _merge_profiles_with_defaults(valid_profiles: dict) -> dict:
    """Merge user profiles with built-in defaults on a per-profile basis."""
    merged = {name: dict(profile) for name, profile in DEFAULT_CONFIG["profiles"].items()}
    for pname, pvalue in valid_profiles.items():
        merged[pname] = {**merged.get(pname, {}), **pvalue}
    return merged


def _resolve_allowed_tools(profile_name: str, profile: dict) -> list[str]:
    """Return the effective whitelist for a profile.

    Semantics:
    - `None`: field absent or invalid -> use built-in defaults for known profiles.
    - `[]`: explicit empty whitelist -> disable all Copilot tools.
    - `[...]`: explicit whitelist -> allow exactly those tools.
    """
    allowed_tools = profile.get("allowed_tools")
    if allowed_tools is not None:
        return allowed_tools
    # Case-insensitive fallback: a user profile named 'Simple' or 'SECURITY'
    # must inherit the same tool restrictions as the built-in 'simple' / 'security'
    # profiles.  Without lowercasing, DEFAULT_CONFIG.get("Simple") returns {} and
    # the caller silently gets [] → all tools enabled (privilege escalation).
    canonical = profile_name.lower()
    return DEFAULT_CONFIG["profiles"].get(canonical, {}).get("allowed_tools", [])


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as exc:
            print(f"[WARNING] Could not load config {CONFIG_FILE}: {exc}", file=sys.stderr)
            return DEFAULT_CONFIG

        if not data:
            return DEFAULT_CONFIG

        if not isinstance(data, dict):
            print(
                f"[WARNING] {CONFIG_FILE}: expected a YAML mapping at top level "
                f"(got {type(data).__name__}), using defaults",
                file=sys.stderr,
            )
            return DEFAULT_CONFIG

        profiles = data.get("profiles")
        if profiles is None:
            print(
                f"[WARNING] {CONFIG_FILE}: 'profiles' key missing, using defaults",
                file=sys.stderr,
            )
            return DEFAULT_CONFIG

        if not isinstance(profiles, dict):
            print(
                f"[WARNING] {CONFIG_FILE}: 'profiles' must be a mapping "
                f"(got {type(profiles).__name__}), using defaults",
                file=sys.stderr,
            )
            return DEFAULT_CONFIG

        # Validate each profile key and value; drop invalid ones with a warning so
        # that one malformed entry in a user-edited config does not crash the server.
        valid_profiles: dict = {}
        for pname, pvalue in profiles.items():
            if not isinstance(pname, str):
                print(
                    f"[WARNING] {CONFIG_FILE}: profile key '{pname}' must be a string "
                    f"(got {type(pname).__name__}), skipping",
                    file=sys.stderr,
                )
                continue
            if not isinstance(pvalue, dict):
                kind = type(pvalue).__name__ if pvalue is not None else "null"
                print(
                    f"[WARNING] {CONFIG_FILE}: profile '{pname}' must be a mapping "
                    f"(got {kind}), skipping",
                    file=sys.stderr,
                )
                continue
            valid_profiles[pname] = _coerce_profile_fields(pvalue, pname)

        if not valid_profiles:
            print(
                f"[WARNING] {CONFIG_FILE}: no valid profiles found, using defaults",
                file=sys.stderr,
            )
            return DEFAULT_CONFIG

        # Merge user profiles into built-in defaults per profile so a partial
        # override of "security" still inherits its default model/timeout/etc.
        data["profiles"] = _merge_profiles_with_defaults(valid_profiles)
        return data

    return DEFAULT_CONFIG


_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB — rotate to .1 when exceeded
# FastMCP uses asyncio (single event loop thread), but a lock is cheap and
# makes log() safe if the server is ever run with multiple threads.
_log_lock = threading.Lock()

# Concurrency guard to limit the number of concurrent Copilot invocations.
# anyio.Semaphore lazily binds to the active backend on first use, so it is safe
# to create at module level and works under both asyncio and trio runtimes.
_DEFAULT_MAX_CONCURRENCY = 3
_global_semaphore = anyio.Semaphore(_DEFAULT_MAX_CONCURRENCY)

# Semaphore acquire timeout is configurable via COPILOT_SEMAPHORE_TIMEOUT
# so operators can tune it for their workload without changing code.

# Default: 30 s — long enough to absorb brief bursts, short enough to give
# callers fast feedback when the server is genuinely overloaded.
try:
    _SEMAPHORE_TIMEOUT = max(1, int(os.environ.get("COPILOT_SEMAPHORE_TIMEOUT", "30")))
except (TypeError, ValueError):
    print("[WARNING] COPILOT_SEMAPHORE_TIMEOUT invalid, using 30", file=sys.stderr)
    _SEMAPHORE_TIMEOUT = 30

# Structured logging — set COPILOT_LOG_FORMAT=json for machine-parseable output.
# Default "text" produces the existing [timestamp] key=value … format for humans.
# JSON mode emits one JSON object per line, making it easy to pipe into jq,
# aggregate in Loki/Grafana, or feed into audit pipelines.
_LOG_FORMAT: str = os.environ.get("COPILOT_LOG_FORMAT", "text").lower()
if _LOG_FORMAT not in ("text", "json"):
    print(f"[WARNING] COPILOT_LOG_FORMAT={_LOG_FORMAT!r} unknown, using 'text'", file=sys.stderr)
    _LOG_FORMAT = "text"


def _parse_log_fields(msg: str) -> dict:
    """Extract key=value pairs from a log message into a dict.

    Supports simple ``key=value`` tokens and shell-quoted values with spaces
    such as ``err='multi word failure'``.
    Used only in JSON log mode to add structured context alongside 'msg'.
    """
    import shlex
    fields: dict = {}
    remainder_parts = []
    try:
        tokens = shlex.split(msg)
    except ValueError:
        tokens = msg.split()
    for token in tokens:
        if "=" in token and not token.startswith("="):
            k, _, v = token.partition("=")
            if k.replace("_", "").replace("-", "").isalnum():
                fields[k] = v
                continue
        remainder_parts.append(token)
    if remainder_parts:
        fields["msg"] = " ".join(remainder_parts)
    return fields


def _quote_log_value(value: str) -> str:
    """Quote a log value so JSON log parsing preserves embedded spaces."""
    import shlex
    return shlex.quote(value)


def log(msg: str) -> None:
    """Write a log entry to LOG_FILE.

    Format is controlled by the COPILOT_LOG_FORMAT env var:
      - ``text`` (default): ``[2026-01-01T12:00:00] profile=simple success len=42``
      - ``json``: ``{"ts": "2026-01-01T12:00:00", "profile": "simple", "event": "success", "len": "42"}``
    """
    import json as _json
    try:
        ts = datetime.now().isoformat(timespec="seconds")
        log_path = Path(LOG_FILE)
        with _log_lock:
            # Symlink guard: refuse to write if the log path (or its rotation
            # target) is a symlink — prevents a local attacker from redirecting
            # log writes to an arbitrary file via a TOCTOU symlink attack (H7).
            if log_path.is_symlink():
                return
            if log_path.exists() and log_path.stat().st_size > _LOG_MAX_BYTES:
                backup = log_path.with_suffix(".log.1")
                if backup.is_symlink():
                    backup.unlink()
                log_path.replace(backup)

            # Escape control characters to prevent log injection
            def _escape_control(s: str) -> str:
                return s.replace("\n", "\\n").replace("\r", "\\r")

            # Open the log file without following symlinks when supported
            _flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            _nofollow = getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(LOG_FILE, _flags | _nofollow, 0o644)
            except OSError:
                # Fall back to the safe plain open if O_NOFOLLOW is unsupported
                fd = os.open(LOG_FILE, _flags, 0o644)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                if _LOG_FORMAT == "json":
                    entry = {"ts": ts, **_parse_log_fields(msg)}
                    # Escape control characters in all string values
                    for k, v in list(entry.items()):
                        if isinstance(v, str):
                            entry[k] = _escape_control(v)
                    f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
                else:
                    f.write(f"[{ts}] {_escape_control(msg)}\n")
    except OSError:
        pass  # never let logging failures break an otherwise successful request


# ---------------------------------------------------------------------------
# Lightweight confusables map for script-mixing attacks.
# NFKC handles fullwidth Latin and many precomposed forms but does NOT map
# Cyrillic or Greek lookalikes to their Latin equivalents.  This table covers
# the characters most commonly used to bypass ASCII keyword matching.
# ---------------------------------------------------------------------------
_HOMOGLYPH_MAP = str.maketrans({
    # Cyrillic → Latin (lowercase)
    "\u0430": "a",  # а → a
    "\u0435": "e",  # е → e
    "\u043E": "o",  # о → o
    "\u0440": "p",  # р → p
    "\u0441": "c",  # с → c
    "\u0445": "x",  # х → x
    "\u0456": "i",  # і → i  (Ukrainian)
    "\u0455": "s",  # ѕ → s  (Macedonian, rare)
    "\u04BB": "h",  # һ → h
    # Cyrillic → Latin (uppercase variants)
    "\u0410": "a",  # А → A
    "\u0412": "b",  # В → B
    "\u0415": "e",  # Е → E
    "\u041D": "h",  # Н → H
    "\u041E": "o",  # О → O
    "\u0420": "p",  # Р → P
    "\u0421": "c",  # С → C
    "\u0422": "t",  # Т → T
    "\u0423": "y",  # У → Y
    "\u0425": "x",  # Х → X
    # Greek → Latin (lowercase)
    "\u03BF": "o",  # ο → o
    "\u03B1": "a",  # α → a
    "\u03B5": "e",  # ε → e
    "\u03BD": "v",  # ν → v
    # Greek → Latin (uppercase variants)
    "\u0391": "a",  # Α → A
    "\u0392": "b",  # Β → B
    "\u0395": "e",  # Ε → E
    "\u0397": "h",  # Η → H
    "\u0399": "i",  # Ι → I
    "\u039A": "k",  # Κ → K
    "\u039C": "m",  # Μ → M
    "\u039D": "n",  # Ν → N
    "\u039F": "o",  # Ο → O
    "\u03A1": "p",  # Ρ → P
    "\u03A4": "t",  # Τ → T
    "\u03A5": "y",  # Υ → Y
    "\u03A7": "x",  # Χ → X
})


def _normalize_text(text: str) -> str:
    """Normalize text to defeat homoglyph/Unicode bypass attacks.

    Steps applied in order:
    1. NFKC — collapses fullwidth Latin (ｓｅｃｕｒｉｔｙ → security),
       ligatures, and many precomposed forms.
    2. Zero-width / directional control char stripping — removes invisible
       characters that visually split keywords (se\u200bcurity → security).
    3. Homoglyph mapping — maps known Cyrillic and Greek lookalikes to their
       ASCII Latin equivalents so ``sеcurity`` (with Cyrillic е U+0435) is
       correctly blocked.
    4. Lower-case — case-insensitive matching.
    """
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]", "", text)
    text = text.translate(_HOMOGLYPH_MAP)
    return text.lower()


def classify_task(task: str, rejected_keywords: list[str]) -> str:
    t = _normalize_text(task.strip())
    for kw in rejected_keywords:
        if _normalize_text(str(kw)) in t:
            return "reject_complex"
    return "allow"


def _decode(b: bytes | None) -> str:
    """Decode subprocess bytes output, replacing any unmappable characters."""
    return b.decode("utf-8", errors="replace") if b else ""


def make_handler(profile_name: str, profile: dict):
    async def handler(task: str) -> str:
        if not task or not task.strip():
            return "ERROR: Empty task"

        rejected_keywords = profile.get("rejected_keywords", [])
        if classify_task(task, rejected_keywords) != "allow":
            return "ERROR: Task rejected by policy. Use Claude directly for this request."

        if not os.path.exists(WRAPPER):
            log(f"profile={profile_name} wrapper not found: {WRAPPER}")
            return "ERROR: Agent wrapper not found. Check installation."

        model = profile.get("model", "gpt-4o-mini")
        prompt_prefix = profile.get("prompt_prefix", "")
        timeout = profile.get("timeout", 300)
        max_input = profile.get("max_input_length", 5000)
        max_output = profile.get("max_output_length", 16000)
        blocked_patterns = profile.get("blocked_patterns", [])
        # allowed_tools was resolved at registration time (_register_tools)
        # and embedded in the profile dict.  Use it directly — no runtime
        # name-based DEFAULT_CONFIG lookup needed.
        allowed_tools = profile.get("allowed_tools", [])

        cmd = [WRAPPER]
        if model:
            cmd += ["--model", model]
        if prompt_prefix:
            cmd += ["--prompt-prefix", prompt_prefix]
        cmd += ["--max-input", str(max_input), "--max-output", str(max_output), "--timeout", str(timeout)]
        for pattern in blocked_patterns:
            cmd += ["--blocked-pattern", pattern]
        for tool in allowed_tools:
            cmd += ["--allowed-tool", tool]
        # Explicit -- separator prevents a task starting with "-" from being
        # misinterpreted as a flag by the wrapper's argument parser.
        cmd += ["--", task]

        proc = None
        cancelled_exc = anyio.get_cancelled_exc_class()
        # Limit how long a caller waits for a slot — prevents a DoS where
        # 3 long-running tasks block all subsequent callers indefinitely.
        try:
            with anyio.fail_after(_SEMAPHORE_TIMEOUT):
                await _global_semaphore.acquire()
        except TimeoutError:
            log(f"profile={profile_name} semaphore_timeout")
            return "ERROR: Server busy, please retry later"

        try:
            try:
                proc = await anyio.open_process(
                    cmd,
                    stdout=PIPE,
                    stderr=PIPE,
                    # New session so the wrapper and Copilot CLI form their own
                    # process group — we can kill the whole group on timeout.
                    start_new_session=True,
                )
                # +5 s grace: wrapper needs time to log and clean up after its own timeout.
                with anyio.fail_after(timeout + 5):
                    stdout, stderr = await _communicate_process(proc)
            except TimeoutError:
                if proc is not None:
                    try:
                        await _kill_proc(proc)
                    except Exception:
                        pass
                log(f"profile={profile_name} timeout")
                return "ERROR: Copilot wrapper timeout"
            except cancelled_exc:
                # Client disconnected — kill the subprocess group so it does not
                # become an orphan, then re-raise so the backend marks the task cancelled.
                if proc is not None:
                    try:
                        await _kill_proc(proc)
                    except Exception:
                        pass
                log(f"profile={profile_name} cancelled")
                raise  # re-raise so asyncio knows the task was cancelled
            except Exception as e:
                if proc is not None:
                    try:
                        await _kill_proc(proc)
                    except Exception:
                        pass
                # Redact exception details in logs to avoid leaking secrets; return a generic error
                log(
                    f"profile={profile_name} "
                    f"exception={_quote_log_value(_redact(str(e))[:200])}"
                )
                return "ERROR: Failed to call wrapper"
            finally:
                if proc is not None:
                    try:
                        await proc.aclose()
                    except Exception:
                        pass

            if proc.returncode != 0:
                err = (_decode(stderr) or _decode(stdout)).strip()
                log(
                    f"profile={profile_name} rc={proc.returncode} "
                    f"err={_quote_log_value(_redact(err)[:200])}"
                )
                return f"ERROR: Copilot failed: {_redact(err)}"

            out = _decode(stdout).strip()
            # Log any warnings from the wrapper (e.g. output truncation) even on success
            warn_msg = _decode(stderr).strip()
            if warn_msg:
                log(
                    f"profile={profile_name} "
                    f"wrapper_warn={_quote_log_value(_redact(warn_msg)[:200])}"
                )
            log(f"profile={profile_name} success len={len(out)}")
            return out or "ERROR: Empty Copilot response"
        finally:
            _global_semaphore.release()

    handler.__name__ = f"run_agent_{profile_name}"
    handler.__doc__ = profile.get("description", f"Run agent with profile: {profile_name}")
    return handler


def _register_tools() -> None:
    """Load config and register one MCP tool per profile.

    Aborts with a clear error if two profile names sanitize to the same tool
    identifier — this prevents one profile silently shadowing another.

    allowed_tools is resolved here, before handing the profile to
    make_handler, so the handler never needs to do a name-based DEFAULT_CONFIG
    lookup at runtime.  The resolved list is stored back into a profile copy
    so make_handler can use profile.get("allowed_tools") directly.
    """
    config = load_config()
    seen: set[str] = set()
    for _name, _profile in config.get("profiles", {}).items():
        sanitized = sanitize_profile_name(_name)
        if sanitized in seen:
            print(
                f"[ERROR] Profile name collision: '{_name}' sanitizes to '{sanitized}', "
                f"which is already registered. Rename one of the conflicting profiles.",
                file=sys.stderr,
            )
            sys.exit(1)
        seen.add(sanitized)
        # Resolve allowed_tools once at registration time and embed the result
        # in the profile copy passed to the handler.  This eliminates the
        # runtime name-based lookup in _resolve_allowed_tools and ensures the
        # whitelist is stable even if the profile name was sanitized (e.g.
        # "Security" → "security").
        profile_copy = dict(_profile)
        profile_copy["allowed_tools"] = _resolve_allowed_tools(sanitized, _profile)
        mcp.tool()(make_handler(sanitized, profile_copy))


def _run_stdio_server() -> None:
    """Run FastMCP over stdio using the trio backend.

    The bundled mcp package's server/client entrypoints use trio explicitly.
    In practice the default anyio backend path can leave stdio MCP servers
    unresponsive in this environment, which makes Claude report
    "Failed to connect" and no tools appear. Requiring trio here makes the
    runtime deterministic and matches the upstream CLI entrypoints.
    """
    try:
        anyio.run(mcp.run_stdio_async, backend="trio")
    except LookupError:
        print(
            "[ERROR] Missing runtime dependency 'trio'. "
            "Run install-copilot-agent.sh --update to refresh the virtualenv.",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> None:
    _register_tools()
    _run_stdio_server()


if __name__ == "__main__":
    main()
