#!/usr/bin/env python3
"""MCP server that delegates tasks to GitHub Copilot CLI based on config profiles."""
import asyncio
import os
import re
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("copilot-delegate")

# INSTALL_DIR: env var > default home path (used for wrapper, logs, venv)
INSTALL_DIR = os.environ.get(
    "COPILOT_INSTALL_DIR",
    str(Path.home() / ".local/share/ai-agent/copilot"),
)

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


_LOG_REDACT_RE = re.compile(
    r"ghp_[A-Za-z0-9]{10,}"               # GitHub personal access token
    r"|sk-[A-Za-z0-9]{10,}"               # OpenAI secret key
    r"|AKIA[0-9A-Z]{10,}"                 # AWS access key
    # Bearer\s+ covers both space and tab separators; lookbehind only matched space.
    r"|Bearer\s+[A-Za-z0-9._~+/=-]{10,}"  # Bearer token (any whitespace separator)
    r"|eyJ[A-Za-z0-9._-]{10,}"            # JWT / GCP service account token
    r"|ya29\.[A-Za-z0-9._-]{10,}"         # GCP OAuth access token
    r"|AccountKey=[A-Za-z0-9+/]{10,}=*"   # Azure Storage account key
    r"|sig=[A-Za-z0-9%+/]{10,}"           # Azure SAS signature
    r"|(?:api[_-]?key|token|secret|password|passwd)=[^ ,;'\"\t\n]+",
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    """Redact common secret patterns before writing to log."""
    def _sub(m: re.Match) -> str:
        s = m.group(0)
        if s.lower().startswith("bearer"):
            return "Bearer REDACTED"
        if "=" in s:
            k, _ = s.split("=", 1)
            return f"{k}=REDACTED"
        return "[REDACTED]"
    return _LOG_REDACT_RE.sub(_sub, text)


def sanitize_profile_name(name: str) -> str:
    """Convert a profile name to a valid Python/MCP tool identifier."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    # Identifiers must not start with a digit.
    if sanitized and sanitized[0].isdigit():
        sanitized = "_" + sanitized
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
            p[field] = []
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
        empty_count = sum(1 for item in p[field] if not item)
        if empty_count:
            print(
                f"[WARNING] {CONFIG_FILE}: profile '{pname}': '{field}' has {empty_count} "
                f"empty string(s), removing them",
                file=sys.stderr,
            )
            p[field] = [item for item in p[field] if item]

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

        data["profiles"] = valid_profiles
        return data

    return DEFAULT_CONFIG


_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB — rotate to .1 when exceeded
# FastMCP uses asyncio (single event loop thread), but a lock is cheap and
# makes log() safe if the server is ever run with multiple threads.
_log_lock = threading.Lock()

# Concurrency guard to limit the number of concurrent Copilot invocations.
# asyncio.Semaphore is safe to create at module level in Python 3.10+ because
# asyncio no longer binds a Semaphore to a specific event loop at creation time.
# The project requires Python 3.11+, so this is guaranteed safe.
_DEFAULT_MAX_CONCURRENCY = 3
_global_semaphore = asyncio.Semaphore(_DEFAULT_MAX_CONCURRENCY)


def log(msg: str) -> None:
    try:
        ts = datetime.now().isoformat(timespec="seconds")
        log_path = Path(LOG_FILE)
        with _log_lock:
            if log_path.exists() and log_path.stat().st_size > _LOG_MAX_BYTES:
                log_path.replace(log_path.with_suffix(".log.1"))
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass  # never let logging failures break an otherwise successful request


def classify_task(task: str, rejected_keywords: list[str]) -> str:
    t = task.strip().lower()
    for kw in rejected_keywords:
        if str(kw).lower() in t:
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
            return f"ERROR: Wrapper not found: {WRAPPER}"

        model = profile.get("model", "gpt-4o-mini")
        prompt_prefix = profile.get("prompt_prefix", "")
        timeout = profile.get("timeout", 300)
        max_input = profile.get("max_input_length", 5000)
        max_output = profile.get("max_output_length", 16000)
        blocked_patterns = profile.get("blocked_patterns", [])
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
        await _global_semaphore.acquire()
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    # New session so the wrapper and Copilot CLI form their own
                    # process group — we can kill the whole group on timeout.
                    start_new_session=True,
                )
                # +5 s grace: wrapper needs time to log and clean up after its own timeout.
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5)
            except asyncio.TimeoutError:
                if proc is not None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (ProcessLookupError, OSError):
                        pass
                    # Give the process group 5 s to honour SIGTERM, then escalate to SIGKILL.
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except (ProcessLookupError, OSError):
                            pass
                        await proc.wait()
                log(f"profile={profile_name} timeout")
                return "ERROR: Copilot wrapper timeout"
            except Exception as e:
                if proc is not None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (ProcessLookupError, OSError):
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except (asyncio.TimeoutError, Exception):
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except (ProcessLookupError, OSError):
                            pass
                        try:
                            await proc.wait()
                        except Exception:
                            pass
                # Redact exception details in logs to avoid leaking secrets; return a generic error
                log(f"profile={profile_name} exception={_redact(str(e))[:200]}")
                return "ERROR: Failed to call wrapper"

            if proc.returncode != 0:
                err = (_decode(stderr) or _decode(stdout)).strip()
                log(f"profile={profile_name} rc={proc.returncode} err={_redact(err)[:200]}")
                return f"ERROR: Copilot failed: {_redact(err)}"

            out = _decode(stdout).strip()
            # Log any warnings from the wrapper (e.g. output truncation) even on success
            warn_msg = _decode(stderr).strip()
            if warn_msg:
                log(f"profile={profile_name} wrapper_warn={_redact(warn_msg)[:200]}")
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
        mcp.tool()(make_handler(sanitized, _profile))


def main() -> None:
    _register_tools()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
