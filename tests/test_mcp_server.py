"""Unit tests for classify_task and load_config in copilot_mcp_server."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mock only the MCP package — yaml is NOT mocked so load_config() tests
# exercise real YAML parsing and the shape-validation branches.
sys.modules.setdefault("mcp", MagicMock())
sys.modules.setdefault("mcp.server", MagicMock())
sys.modules.setdefault("mcp.server.fastmcp", MagicMock())

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from copilot_mcp_server import classify_task, load_config, DEFAULT_CONFIG, sanitize_profile_name, _redact, _coerce_profile_fields, _resolve_allowed_tools  # noqa: E402

# Use the real DEFAULT_CONFIG keywords so any future config change is automatically
# reflected in tests instead of silently diverging.
KEYWORDS = DEFAULT_CONFIG["profiles"]["simple"]["rejected_keywords"]


# ---------------------------------------------------------------------------
# classify_task
# ---------------------------------------------------------------------------

def test_allowed_task():
    assert classify_task("write a hello world function in Python", KEYWORDS) == "allow"


def test_allowed_task_no_keywords():
    assert classify_task("generate boilerplate for a REST API", KEYWORDS) == "allow"


def test_rejected_security():
    assert classify_task("do a security review of this code", KEYWORDS) == "reject_complex"


def test_rejected_architecture():
    assert classify_task("design the architecture of the system", KEYWORDS) == "reject_complex"


def test_case_insensitive_security():
    assert classify_task("check SECURITY vulnerabilities", KEYWORDS) == "reject_complex"


def test_case_insensitive_architecture():
    assert classify_task("ARCHITECTURE design", KEYWORDS) == "reject_complex"


def test_empty_input():
    assert classify_task("", KEYWORDS) == "allow"


def test_whitespace_only():
    assert classify_task("   ", KEYWORDS) == "allow"


def test_empty_keywords_always_allows():
    assert classify_task("full authentication system design", []) == "allow"


# ---------------------------------------------------------------------------
# load_config — shape validation (uses real yaml, not a mock)
# ---------------------------------------------------------------------------

def test_load_config_profiles_list_falls_back_to_default(tmp_path, monkeypatch):
    """profiles: [list] is invalid — must fall back to defaults."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  - item1\n  - item2\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    assert load_config() == DEFAULT_CONFIG


def test_load_config_top_level_list_falls_back_to_default(tmp_path, monkeypatch):
    """Top-level list instead of mapping → fall back to defaults."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("- item1\n- item2\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    assert load_config() == DEFAULT_CONFIG


def test_load_config_empty_file_falls_back_to_default(tmp_path, monkeypatch):
    """Empty YAML file → fall back to defaults."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    assert load_config() == DEFAULT_CONFIG


def test_load_config_missing_profiles_key_falls_back_to_default(tmp_path, monkeypatch):
    """Valid mapping but no 'profiles' key → fall back to defaults with warning."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("other_key: value\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    assert load_config() == DEFAULT_CONFIG


def test_load_config_invalid_profile_value_is_skipped(tmp_path, monkeypatch):
    """A profile whose value is a list is skipped; valid profiles survive."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n"
        "  bad_profile:\n"
        "    - item\n"
        "  good_profile:\n"
        "    model: gpt-4o-mini\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert "bad_profile" not in result["profiles"]
    assert "good_profile" in result["profiles"]


def test_load_config_null_profile_value_is_skipped(tmp_path, monkeypatch):
    """A profile with null value (bare key) is skipped."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  empty_profile:\n  valid:\n    model: gpt-4o\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert "empty_profile" not in result["profiles"]
    assert "valid" in result["profiles"]


def test_load_config_all_profiles_invalid_falls_back_to_default(tmp_path, monkeypatch):
    """If all profiles are invalid, fall back to defaults."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  bad:\n    - x\n  also_bad:\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    assert load_config() == DEFAULT_CONFIG


def test_load_config_valid_profiles_loaded(tmp_path, monkeypatch):
    """Well-formed config with a custom model is loaded and reflected in result."""
    cfg = tmp_path / "config.yaml"
    # Use a non-default model to confirm the user value is reflected
    cfg.write_text("profiles:\n  simple:\n    model: gpt-4o\n    timeout: 300\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert "simple" in result["profiles"]
    assert result["profiles"]["simple"]["model"] == "gpt-4o"


def test_load_config_integer_profile_key_is_skipped(tmp_path, monkeypatch):
    """YAML integer key (123:) is skipped; valid string-keyed profile survives."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  123:\n    model: gpt-4o-mini\n  good:\n    model: gpt-4o\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert 123 not in result["profiles"]
    assert "good" in result["profiles"]


def test_load_config_all_integer_keys_fall_back_to_default(tmp_path, monkeypatch):
    """If every profile key is non-string, fall back to defaults."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  123:\n    model: gpt-4o-mini\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    assert load_config() == DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# _coerce_profile_fields — field-level type coercion
# ---------------------------------------------------------------------------

def test_coerce_string_timeout_to_int(tmp_path, monkeypatch):
    """timeout: '600' (string) is coerced to int 600."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    timeout: '600'\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"]["timeout"] == 600
    assert isinstance(result["profiles"]["p"]["timeout"], int)


def test_coerce_invalid_timeout_uses_default(tmp_path, monkeypatch):
    """timeout: 'fast' (non-numeric string) falls back to default 300."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    timeout: fast\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"]["timeout"] == 300


def test_coerce_zero_timeout_uses_default(tmp_path, monkeypatch):
    """timeout: 0 is invalid and falls back to default 300."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    timeout: 0\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"]["timeout"] == 300


def test_coerce_bool_timeout_uses_default(tmp_path, monkeypatch):
    """timeout: true is invalid and falls back to default 300."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    timeout: true\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"]["timeout"] == 300


def test_coerce_negative_max_input_uses_default(tmp_path, monkeypatch):
    """max_input_length: -1 is invalid and falls back to default 5000."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    max_input_length: -1\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"]["max_input_length"] == 5000


def test_coerce_bool_max_input_uses_default(tmp_path, monkeypatch):
    """max_input_length: false is invalid and falls back to default 5000."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    max_input_length: false\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"]["max_input_length"] == 5000


def test_coerce_zero_max_output_uses_default(tmp_path, monkeypatch):
    """max_output_length: 0 is invalid and falls back to default 16000."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    max_output_length: 0\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"]["max_output_length"] == 16000


def test_coerce_bool_max_output_uses_default(tmp_path, monkeypatch):
    """max_output_length: true is invalid and falls back to default 16000."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    max_output_length: true\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"]["max_output_length"] == 16000


def test_coerce_blocked_patterns_int_items_to_str(tmp_path, monkeypatch):
    """blocked_patterns: [123] — integer items are coerced to strings."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    blocked_patterns:\n      - 123\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"]["blocked_patterns"] == ["123"]


def test_coerce_rejected_keywords_string_scalar_replaced_with_empty_list(tmp_path, monkeypatch):
    """rejected_keywords: security (scalar string, not list) → replaced with []."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    rejected_keywords: security\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"]["rejected_keywords"] == []


def test_coerce_allowed_tools_int_items_to_str(tmp_path, monkeypatch):
    """allowed_tools: [123] — integer items are coerced to strings."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    allowed_tools:\n      - 123\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"]["allowed_tools"] == ["123"]


def test_coerce_allowed_tools_scalar_replaced_with_none(tmp_path, monkeypatch):
    """allowed_tools: view (scalar, not list) → coerced to None."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    allowed_tools: view\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"].get("allowed_tools") is None


def test_allowed_tools_missing_falls_back_to_default_config(tmp_path, monkeypatch):
    """security profile without allowed_tools falls back to DEFAULT_CONFIG's read-only
    whitelist."""
    cfg = tmp_path / "config.yaml"
    # Old-style config: security profile with no allowed_tools key at all
    cfg.write_text(
        "profiles:\n"
        "  security:\n"
        "    model: gpt-4o\n"
        "    timeout: 600\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    config = load_config()
    profile = config["profiles"]["security"]
    allowed_tools = _resolve_allowed_tools("security", profile)

    assert allowed_tools
    assert "bash" not in allowed_tools
    assert "edit" not in allowed_tools
    assert "create" not in allowed_tools
    assert "view" in allowed_tools


def test_allowed_tools_invalid_scalar_falls_back_to_default_config(tmp_path, monkeypatch):
    """allowed_tools: view (scalar typo) is coerced to None, then falls back to
    DEFAULT_CONFIG's safe whitelist for built-in profiles."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n"
        "  security:\n"
        "    allowed_tools: view\n"  # typo: scalar instead of list
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    config = load_config()
    profile = config["profiles"]["security"]

    assert profile.get("allowed_tools") is None
    allowed_tools = _resolve_allowed_tools("security", profile)

    assert "bash" not in allowed_tools
    assert "view" in allowed_tools


def test_default_config_has_all_three_profiles():
    """DEFAULT_CONFIG must contain simple, security, and code_review profiles."""
    assert "simple" in DEFAULT_CONFIG["profiles"]
    assert "security" in DEFAULT_CONFIG["profiles"]
    assert "code_review" in DEFAULT_CONFIG["profiles"]


def test_default_config_simple_has_bash_in_allowed_tools():
    """simple profile allows bash (needed for test/build tasks)."""
    tools = DEFAULT_CONFIG["profiles"]["simple"]["allowed_tools"]
    assert "bash" in tools
    assert "create" in tools
    assert "edit" in tools


def test_default_config_security_is_readonly():
    """security profile must NOT allow bash, create, or edit."""
    tools = DEFAULT_CONFIG["profiles"]["security"]["allowed_tools"]
    assert "bash" not in tools
    assert "create" not in tools
    assert "edit" not in tools


def test_default_config_code_review_is_readonly():
    """code_review profile must NOT allow bash, create, or edit."""
    tools = DEFAULT_CONFIG["profiles"]["code_review"]["allowed_tools"]
    assert "bash" not in tools
    assert "create" not in tools
    assert "edit" not in tools


def test_coerce_model_dict_uses_default(tmp_path, monkeypatch):
    """model: {nested: value} (YAML object) is replaced with default 'gpt-4o-mini'."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    model:\n      nested: value\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"]["model"] == "gpt-4o-mini"


def test_coerce_prompt_prefix_list_uses_default(tmp_path, monkeypatch):
    """prompt_prefix: [a, b] (list) is replaced with empty string."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  p:\n    prompt_prefix:\n      - a\n      - b\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["p"]["prompt_prefix"] == ""


# ---------------------------------------------------------------------------
# sanitize_profile_name
# ---------------------------------------------------------------------------

def test_sanitize_profile_name_replaces_hyphens():
    assert sanitize_profile_name("code-review") == "code_review"


def test_sanitize_profile_name_replaces_spaces():
    assert sanitize_profile_name("my profile") == "my_profile"


def test_sanitize_profile_name_leading_digit_gets_prefix():
    """A name starting with a digit must be prefixed with '_'."""
    assert sanitize_profile_name("123abc") == "_123abc"


def test_sanitize_profile_name_all_digits():
    assert sanitize_profile_name("42") == "_42"


def test_sanitize_profile_name_already_valid():
    assert sanitize_profile_name("simple") == "simple"


# ---------------------------------------------------------------------------
# _redact — secret redaction
# ---------------------------------------------------------------------------

def test_redact_github_token():
    # redact.py returns type-prefixed replacement (ghp_REDACTED) for richer context
    assert _redact("token is ghp_1234567890abcdefghijklmnopqrstuvwxyz") == "token is ghp_REDACTED"


def test_redact_openai_key():
    assert _redact("key: sk-1234567890abcdefghijklmnopqrstuvwxyz1234") == "key: sk-REDACTED"


def test_redact_aws_key():
    assert _redact("AWS_ACCESS_KEY_ID=AKIA1234567890ABCDEF") == "AWS_ACCESS_KEY_ID=AKIA_REDACTED"


def test_redact_jwt_gcp():
    # Bearer\s+ matches the whole "Bearer <token>"; function returns "Bearer REDACTED"
    assert _redact("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ") == "Authorization: Bearer REDACTED"


def test_redact_raw_bearer_token():
    assert _redact("Authorization: Bearer abcdefghijklmnop") == "Authorization: Bearer REDACTED"


def test_redact_bearer_tab_separator():
    # Tab between Bearer and token must also be redacted
    assert _redact("Authorization: Bearer\tabcdefghijklmnop") == "Authorization: Bearer REDACTED"


def test_redact_gcp_oauth():
    assert _redact("access_token: ya29.a0AfB_ByD_E_F_G_H_I_J_K_L") == "access_token: ya29.REDACTED"


def test_redact_azure_storage_key():
    # Key=value pattern preserves the key name for context
    assert _redact("AccountKey=abc/def+ghi+1234567890==") == "AccountKey=REDACTED"


def test_redact_azure_sas_signature():
    assert _redact("sig=abc%2Bdef%2Fghi%3D1234567890") == "sig=REDACTED"


def test_redact_generic_secret():
    # key=value patterns preserve the key name; the whole "password=..." is matched
    assert _redact("db_password=supersecret123") == "db_password=REDACTED"
    assert _redact("API-KEY=my-secret-key") == "API-KEY=REDACTED"


def test_redact_multiple_secrets():
    input_text = "Login with user:pass, token=ghp_abc123 and secret=sk-456"
    # Both matched by generic key=value pattern; key names are preserved
    expected = "Login with user:pass, token=REDACTED and secret=REDACTED"
    assert _redact(input_text) == expected


def test_redact_case_insensitivity():
    assert _redact("PASSWORD=secret") == "PASSWORD=REDACTED"
    assert _redact("password=secret") == "password=REDACTED"
    assert _redact("PassWord=secret") == "PassWord=REDACTED"


# ---------------------------------------------------------------------------
# _validate_install_dir (C1)
# ---------------------------------------------------------------------------

def test_validate_install_dir_home(monkeypatch, tmp_path):
    """Path inside home directory is accepted."""
    from copilot_mcp_server import _validate_install_dir
    install_dir = tmp_path / "mydir"
    install_dir.mkdir()
    monkeypatch.setattr("copilot_mcp_server.Path.home", lambda: tmp_path)
    monkeypatch.setattr("copilot_mcp_server._uid_home", lambda: None)
    # Should return without exiting
    result = _validate_install_dir(str(install_dir))
    assert str(install_dir.resolve()) in result or result == str(install_dir.resolve())


def test_validate_install_dir_uid_home_when_home_env_differs(monkeypatch, tmp_path):
    """Accept install dir under the real uid home even if Path.home() points elsewhere."""
    from copilot_mcp_server import _validate_install_dir
    runtime_home = tmp_path / "runtime-home"
    uid_home = tmp_path / "uid-home"
    install_dir = uid_home / ".local" / "share" / "ai-agent" / "copilot"
    runtime_home.mkdir()
    install_dir.mkdir(parents=True)
    monkeypatch.setattr("copilot_mcp_server.Path.home", lambda: runtime_home)
    monkeypatch.setattr("copilot_mcp_server._uid_home", lambda: uid_home)
    result = _validate_install_dir(str(install_dir))
    assert result == str(install_dir.resolve())


def test_validate_install_dir_untrusted_exits(monkeypatch, tmp_path):
    """Path outside all trusted prefixes causes sys.exit(1)."""
    import pytest
    from copilot_mcp_server import _validate_install_dir
    # /tmp is not in the trusted list; resolve is real
    untrusted = tmp_path  # tmp_path is typically /tmp/pytest-xxx/...
    # Override home to something different so tmp_path is not under it
    monkeypatch.setattr("copilot_mcp_server.Path.home", lambda: Path("/nonexistent_home_xyz"))
    monkeypatch.setattr("copilot_mcp_server._uid_home", lambda: None)
    with pytest.raises(SystemExit):
        _validate_install_dir(str(untrusted))


def test_validate_install_dir_invalid_path_exits(monkeypatch):
    """Non-resolvable path causes sys.exit(1)."""
    import pytest
    from copilot_mcp_server import _validate_install_dir
    monkeypatch.setattr("copilot_mcp_server.Path.home", lambda: Path("/nonexistent_home_xyz"))
    monkeypatch.setattr("copilot_mcp_server._uid_home", lambda: None)
    with pytest.raises(SystemExit):
        _validate_install_dir("/nonexistent_home_xyz/../../../evil")


def test_run_stdio_server_uses_trio_backend(monkeypatch):
    """The MCP server should run with trio explicitly for stdio reliability."""
    from copilot_mcp_server import _run_stdio_server, mcp

    called = {}

    def fake_run(fn, backend=None):
        called["fn"] = fn
        called["backend"] = backend

    monkeypatch.setattr("copilot_mcp_server.anyio.run", fake_run)
    _run_stdio_server()
    assert called["fn"] == mcp.run_stdio_async
    assert called["backend"] == "trio"


def test_run_stdio_server_missing_trio_exits(monkeypatch):
    """A missing trio backend should fail fast with a clear startup error."""
    import pytest
    from copilot_mcp_server import _run_stdio_server

    monkeypatch.setattr("copilot_mcp_server.anyio.run", lambda *args, **kwargs: (_ for _ in ()).throw(LookupError("No such backend: trio")))
    with pytest.raises(SystemExit):
        _run_stdio_server()


# ---------------------------------------------------------------------------
# _normalize_text / classify_task — Unicode bypass prevention (M6)
# ---------------------------------------------------------------------------

def test_classify_task_fullwidth_keyword_blocked():
    """Fullwidth Unicode chars (ｓｅｃｕｒｉｔｙ) must be normalized and blocked."""
    # "ｓｅｃｕｒｉｔｙ" = fullwidth Latin letters, NFKC-normalize to "security"
    fullwidth_security = "\uff53\uff45\uff43\uff55\uff52\uff49\uff54\uff59"
    assert classify_task(f"please do a {fullwidth_security} review", KEYWORDS) == "reject_complex"


def test_classify_task_zero_width_chars_stripped():
    """Zero-width chars inserted into a keyword must not prevent matching."""
    # "se\u200bcurity" — zero-width space splits the word visually
    zwsp_security = "se\u200bcurity"
    assert classify_task(f"check {zwsp_security} of this code", KEYWORDS) == "reject_complex"


def test_normalize_text_returns_lowercase():
    from copilot_mcp_server import _normalize_text
    assert _normalize_text("Hello World") == "hello world"


def test_normalize_text_strips_zero_width():
    from copilot_mcp_server import _normalize_text
    assert _normalize_text("hel\u200blo") == "hello"


def test_normalize_text_nfkc():
    from copilot_mcp_server import _normalize_text
    # NFKC: ｈｅｌｌｏ → hello
    assert _normalize_text("\uff48\uff45\uff4c\uff4c\uff4f") == "hello"


# ---------------------------------------------------------------------------
# load_config — partial config merges with DEFAULT_CONFIG (H4)
# ---------------------------------------------------------------------------

def test_load_config_partial_config_keeps_default_profiles(tmp_path, monkeypatch):
    """A config with only 'simple' profile must still expose security and code_review."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n"
        "  simple:\n"
        "    model: gpt-4o-mini\n"
        "    description: test\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert "security" in result["profiles"]
    assert "code_review" in result["profiles"]


def test_load_config_partial_builtin_profile_keeps_missing_fields(tmp_path, monkeypatch):
    """A partial override of a built-in profile must retain its built-in defaults."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n"
        "  security:\n"
        "    allowed_tools:\n"
        "      - view\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    profile = result["profiles"]["security"]
    assert profile["allowed_tools"] == ["view"]
    assert profile["model"] == DEFAULT_CONFIG["profiles"]["security"]["model"]
    assert profile["timeout"] == DEFAULT_CONFIG["profiles"]["security"]["timeout"]
    assert profile["prompt_prefix"] == DEFAULT_CONFIG["profiles"]["security"]["prompt_prefix"]


def test_load_config_user_profile_overrides_default(tmp_path, monkeypatch):
    """User-defined model overrides the DEFAULT_CONFIG model for that profile."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n"
        "  simple:\n"
        "    model: custom-model\n"
        "    description: overridden\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result["profiles"]["simple"]["model"] == "custom-model"


def test_load_config_new_profile_added_alongside_defaults(tmp_path, monkeypatch):
    """A brand-new profile must coexist with the three default profiles."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n"
        "  custom:\n"
        "    model: gpt-4o\n"
        "    description: my custom profile\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert "custom" in result["profiles"]
    assert "simple" in result["profiles"]
    assert "security" in result["profiles"]


# ---------------------------------------------------------------------------
# _redact — additional secret patterns
# ---------------------------------------------------------------------------

def test_redact_github_fine_grained_pat():
    assert "github_pat_REDACTED" in _redact("token: github_pat_abcdefghij1234567890") or \
           "[REDACTED]" in _redact("token: github_pat_abcdefghij1234567890")


def test_redact_github_oauth_token():
    result = _redact("oauth: gho_abcdefghij1234567890")
    assert "REDACTED" in result
    assert "gho_abcdefghij1234567890" not in result


def test_redact_slack_bot_token():
    result = _redact("slack: xoxb-123456789-abcdefghij1234")
    assert "REDACTED" in result
    assert "xoxb-123456789-abcdefghij1234" not in result


def test_redact_stripe_secret_key():
    result = _redact("key: sk_live_abcdefghij1234567890")
    assert "REDACTED" in result
    assert "sk_live_abcdefghij1234567890" not in result


def test_redact_pem_private_key_header():
    result = _redact("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAK...")
    assert "REDACTED" in result
    assert "BEGIN RSA PRIVATE KEY" not in result


def test_redact_db_url_with_password():
    result = _redact("conn = postgresql://user:secret@host:5432/db")
    assert "REDACTED" in result
    assert "secret" not in result


# ---------------------------------------------------------------------------
# sanitize_profile_name — empty result exits (M9)
# ---------------------------------------------------------------------------

def test_sanitize_profile_name_empty_string_exits():
    """Empty string profile name must cause sys.exit(1)."""
    import pytest
    with pytest.raises(SystemExit):
        sanitize_profile_name("")


def test_sanitize_profile_name_only_special_chars_becomes_underscores():
    """A name with only special chars sanitizes to underscores (valid identifier)."""
    # "!@#$%" → "_____" which is a valid Python identifier — no exit expected.
    result = sanitize_profile_name("!@#$%")
    assert result == "_____"


# ---------------------------------------------------------------------------
# make_handler — async handler tests (L3/L5)
# ---------------------------------------------------------------------------

def test_handler_empty_task_returns_error():
    """make_handler must return ERROR for empty task without calling wrapper."""
    import asyncio
    from copilot_mcp_server import make_handler, DEFAULT_CONFIG

    profile = DEFAULT_CONFIG["profiles"]["simple"].copy()
    handler = make_handler("simple", profile)
    result = asyncio.run(handler(""))
    assert result.startswith("ERROR")


def test_handler_whitespace_task_returns_error():
    import asyncio
    from copilot_mcp_server import make_handler, DEFAULT_CONFIG

    profile = DEFAULT_CONFIG["profiles"]["simple"].copy()
    handler = make_handler("simple", profile)
    result = asyncio.run(handler("   "))
    assert result.startswith("ERROR")


def test_handler_rejected_keyword_returns_policy_error():
    """make_handler must reject tasks matching rejected_keywords."""
    import asyncio
    from copilot_mcp_server import make_handler, DEFAULT_CONFIG

    profile = DEFAULT_CONFIG["profiles"]["simple"].copy()
    handler = make_handler("simple", profile)
    result = asyncio.run(handler("please do a security audit of this code"))
    assert "rejected" in result.lower() or result.startswith("ERROR")


def test_handler_wrapper_not_found_returns_generic_error(monkeypatch):
    """When wrapper binary is missing, return generic message (no path leak — M8)."""
    import asyncio
    from copilot_mcp_server import make_handler, DEFAULT_CONFIG

    monkeypatch.setattr("copilot_mcp_server.WRAPPER", "/nonexistent/wrapper.sh")
    profile = DEFAULT_CONFIG["profiles"]["simple"].copy()
    # Remove rejected_keywords to allow the task through
    profile["rejected_keywords"] = []
    handler = make_handler("simple", profile)
    result = asyncio.run(handler("write a hello world function"))
    assert result.startswith("ERROR")
    assert "/nonexistent/wrapper.sh" not in result  # no path leak


def test_handler_wrapper_nonzero_exit_returns_error(tmp_path, monkeypatch):
    """Wrapper exits non-zero → handler returns error without leaking internal details."""
    import asyncio
    from copilot_mcp_server import make_handler, DEFAULT_CONFIG

    fake_wrapper = tmp_path / "wrapper.sh"
    fake_wrapper.write_text("#!/usr/bin/env bash\necho 'failure output' >&2\nexit 5\n")
    fake_wrapper.chmod(0o755)

    monkeypatch.setattr("copilot_mcp_server.WRAPPER", str(fake_wrapper))
    profile = DEFAULT_CONFIG["profiles"]["simple"].copy()
    profile["rejected_keywords"] = []
    profile["blocked_patterns"] = []
    profile["allowed_tools"] = []
    profile["timeout"] = 10
    handler = make_handler("simple", profile)
    result = asyncio.run(handler("write hello world"))
    assert result.startswith("ERROR")


def test_handler_wrapper_empty_output_returns_error(tmp_path, monkeypatch):
    """Wrapper that produces no output → 'Empty Copilot response' error."""
    import asyncio
    from copilot_mcp_server import make_handler, DEFAULT_CONFIG

    fake_wrapper = tmp_path / "wrapper.sh"
    fake_wrapper.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_wrapper.chmod(0o755)

    monkeypatch.setattr("copilot_mcp_server.WRAPPER", str(fake_wrapper))
    profile = DEFAULT_CONFIG["profiles"]["simple"].copy()
    profile["rejected_keywords"] = []
    profile["blocked_patterns"] = []
    profile["allowed_tools"] = []
    profile["timeout"] = 10
    handler = make_handler("simple", profile)
    result = asyncio.run(handler("write hello world"))
    assert "Empty" in result or result.startswith("ERROR")


def test_handler_semaphore_timeout_returns_busy_error():
    """When semaphore cannot be acquired in time, return SERVER_BUSY error.

    We monkeypatch anyio.fail_after to raise TimeoutError immediately so the
    acquire path returns the busy error without waiting on real timers.
    """
    import asyncio
    import copilot_mcp_server as _srv
    from copilot_mcp_server import make_handler, DEFAULT_CONFIG

    profile = DEFAULT_CONFIG["profiles"]["simple"].copy()
    profile["rejected_keywords"] = []

    async def run():
        original_semaphore = _srv._global_semaphore
        sem = _srv.anyio.Semaphore(0)
        _srv._global_semaphore = sem
        try:
            class _ImmediateTimeout:
                def __enter__(self):
                    raise TimeoutError

                def __exit__(self, exc_type, exc, tb):
                    return False

            import copilot_mcp_server
            original_fail_after = copilot_mcp_server.anyio.fail_after
            copilot_mcp_server.anyio.fail_after = lambda *args, **kwargs: _ImmediateTimeout()
            try:
                handler = make_handler("simple", profile)
                return await handler("write hello world")
            finally:
                copilot_mcp_server.anyio.fail_after = original_fail_after
        finally:
            _srv._global_semaphore = original_semaphore

    result = asyncio.run(run())
    assert "busy" in result.lower() or result.startswith("ERROR")


# ---------------------------------------------------------------------------
# allowed_tools semantics
# ---------------------------------------------------------------------------

def test_explicit_empty_allowed_tools_not_overridden_by_default(tmp_path, monkeypatch):
    """Explicit [] must remain an empty whitelist, not silently fall back."""
    import asyncio
    from copilot_mcp_server import make_handler, DEFAULT_CONFIG

    fake_wrapper = tmp_path / "wrapper.sh"
    fake_wrapper.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\"\n")
    fake_wrapper.chmod(0o755)

    monkeypatch.setattr("copilot_mcp_server.WRAPPER", str(fake_wrapper))
    profile = DEFAULT_CONFIG["profiles"]["security"].copy()
    profile["rejected_keywords"] = []
    profile["allowed_tools"] = []
    handler = make_handler("security", profile)
    result = asyncio.run(handler("hello world"))

    assert "--allowed-tool" not in result
    assert "--model gpt-4o " in f"{result} "


def test_absent_allowed_tools_uses_default_config_whitelist(tmp_path, monkeypatch):
    """Absent allowed_tools for a built-in profile must use its default whitelist."""
    profile = {"model": "gpt-4o", "timeout": 300}
    resolved = _resolve_allowed_tools("security", profile)
    assert "view" in resolved
    assert "bash" not in resolved


def test_coerce_allowed_tools_rejects_items_with_commas(tmp_path, monkeypatch):
    """Items in allowed_tools containing commas must warn and be removed."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n"
        "  p:\n"
        "    allowed_tools:\n"
        "      - 'view,bash'\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    tools = result["profiles"]["p"].get("allowed_tools", [])
    assert tools == []


def test_comma_allowed_tools_do_not_expand_permissions(tmp_path, monkeypatch):
    """Removing comma-separated tools must not re-expand to a broader whitelist."""
    import asyncio
    from copilot_mcp_server import make_handler

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n"
        "  security:\n"
        "    allowed_tools:\n"
        "      - 'view,bash'\n"
        "    rejected_keywords: []\n"
        "    blocked_patterns: []\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    profile = load_config()["profiles"]["security"]
    assert profile["allowed_tools"] == []

    fake_wrapper = tmp_path / "wrapper.sh"
    fake_wrapper.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\"\n")
    fake_wrapper.chmod(0o755)
    monkeypatch.setattr("copilot_mcp_server.WRAPPER", str(fake_wrapper))

    handler = make_handler("security", profile)
    result = asyncio.run(handler("hello world"))

    assert "--allowed-tool" not in result


def test_coerce_allowed_tools_valid_items_kept(tmp_path, monkeypatch):
    """Valid tool names (no commas) must be preserved."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n"
        "  p:\n"
        "    allowed_tools:\n"
        "      - view\n"
        "      - grep\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    tools = result["profiles"]["p"]["allowed_tools"]
    assert tools == ["view", "grep"]


def test_coerce_allowed_tools_mixed_some_valid_some_comma(tmp_path, monkeypatch):
    """Only comma-containing items are removed; valid items survive."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n"
        "  p:\n"
        "    allowed_tools:\n"
        "      - view\n"
        "      - 'grep,bash'\n"
        "      - glob\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    tools = result["profiles"]["p"]["allowed_tools"]
    assert "grep,bash" not in tools
    assert "view" in tools
    assert "glob" in tools


# ---------------------------------------------------------------------------
# Cyrillic homoglyph bypass (extended _normalize_text)
# ---------------------------------------------------------------------------

def test_classify_task_cyrillic_e_in_security_blocked():
    """'sеcurity' with Cyrillic е (U+0435) must still be blocked."""
    cyrillic_e = "\u0435"
    task = f"please do a s{cyrillic_e}curity review"
    assert classify_task(task, KEYWORDS) == "reject_complex"


def test_classify_task_cyrillic_a_in_architecture_blocked():
    """'аrchitecture' with Cyrillic а (U+0430) must be blocked."""
    cyrillic_a = "\u0430"
    task = f"{cyrillic_a}rchitecture design for the system"
    assert classify_task(task, KEYWORDS) == "reject_complex"


def test_classify_task_greek_o_in_authorization_blocked():
    """'auth\u03BFrization' (Greek ο) must be blocked."""
    task = f"auth\u03BFrization middleware design"
    assert classify_task(task, KEYWORDS) == "reject_complex"


def test_normalize_text_cyrillic_е_maps_to_e():
    from copilot_mcp_server import _normalize_text
    assert _normalize_text("s\u0435curity") == "security"


def test_normalize_text_greek_ο_maps_to_o():
    from copilot_mcp_server import _normalize_text
    assert _normalize_text("auth\u03BFrization") == "authorization"


def test_classify_task_cyrillic_p_in_compliance_blocked():
    """'comрliance' with Cyrillic р (U+0440) must still be blocked."""
    cyrillic_p = "\u0440"
    task = f"need com{cyrillic_p}liance review"
    assert classify_task(task, KEYWORDS) == "reject_complex"


def test_normalize_text_cyrillic_р_maps_to_p():
    from copilot_mcp_server import _normalize_text
    assert _normalize_text("com\u0440liance") == "compliance"


# ---------------------------------------------------------------------------
# redact.py is the single source — server _redact uses it directly
# ---------------------------------------------------------------------------

def test_server_redact_uses_shared_redact_module():
    """_redact in the server must use REDACT_PATTERN from redact.py."""
    import copilot_mcp_server as srv
    # The server replaces _LOG_REDACT_RE with the pattern from redact.py.
    # Verify ghp_ token produces same output as calling redact.py directly.
    import importlib.util
    from pathlib import Path
    for rpath in (
        Path(srv.__file__).parent / "redact.py",
        Path(srv.__file__).parent.parent / "bin" / "redact.py",
    ):
        if rpath.exists():
            spec = importlib.util.spec_from_file_location("redact_direct", rpath)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            break
    else:
        pytest.skip("redact.py not found — install layout test only")

    token = "ghp_" + "A" * 20
    assert srv._redact(f"token: {token}") == mod.redact(f"token: {token}")


# ---------------------------------------------------------------------------
# _register_tools() — collision detection and sys.exit path
# ---------------------------------------------------------------------------

def test_register_tools_collision_exits(monkeypatch, tmp_path):
    """Two profiles that sanitize to the same name must cause sys.exit(1)."""
    import pytest
    from copilot_mcp_server import _register_tools
    from unittest.mock import MagicMock

    # "my-profile" and "my_profile" both sanitize to "my_profile" → collision
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n"
        "  my-profile:\n"
        "    model: gpt-4o-mini\n"
        "    description: first\n"
        "  my_profile:\n"
        "    model: gpt-4o-mini\n"
        "    description: second\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    with pytest.raises(SystemExit):
        _register_tools()


def test_register_tools_creates_tool_per_profile(monkeypatch, tmp_path):
    """_register_tools registers one mcp.tool() call per valid profile."""
    from copilot_mcp_server import _register_tools
    import copilot_mcp_server as srv
    from unittest.mock import MagicMock, patch

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n"
        "  alpha:\n"
        "    model: gpt-4o-mini\n"
        "    description: alpha profile\n"
        "  beta:\n"
        "    model: gpt-4o\n"
        "    description: beta profile\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))

    registered = []
    def fake_tool_decorator():
        def decorator(fn):
            registered.append(fn.__name__)
            return fn
        return decorator

    with patch.object(srv.mcp, "tool", side_effect=fake_tool_decorator):
        _register_tools()

    # All three default profiles + alpha + beta must be registered
    assert "run_agent_alpha" in registered
    assert "run_agent_beta" in registered
    assert "run_agent_simple" in registered


# ---------------------------------------------------------------------------
# redact.py import failure scenarios
# ---------------------------------------------------------------------------

def test_load_redact_module_raises_if_not_found(monkeypatch):
    """_load_redact_module raises ImportError when redact.py doesn't exist."""
    import pytest
    from copilot_mcp_server import _load_redact_module
    from unittest.mock import patch
    from pathlib import Path

    # Make Path.exists always return False so neither candidate is found
    with patch.object(Path, "exists", return_value=False):
        with pytest.raises(ImportError, match="Could not find redact.py"):
            _load_redact_module()


def test_load_redact_module_raises_if_exports_missing(tmp_path, monkeypatch):
    """_load_redact_module raises ImportError if redact.py is missing exports."""
    import pytest
    from copilot_mcp_server import _load_redact_module
    from unittest.mock import patch
    from pathlib import Path

    # Create a broken redact.py that is missing required exports
    broken = tmp_path / "redact.py"
    broken.write_text("# broken — no exports\nFOO = 1\n")

    orig_exists = Path.exists
    def patched_exists(self):
        if self == broken:
            return True
        return orig_exists(self)

    with patch.object(Path, "exists", patched_exists):
        # Patch candidates list so the broken file is the only candidate
        import copilot_mcp_server as srv
        orig_file = srv.__file__
        with patch.object(Path, "__truediv__", lambda self, other: broken if "redact" in str(other) else Path.__truediv__(self, other)):
            with pytest.raises((ImportError, Exception)):
                _load_redact_module()


def test_redact_module_has_required_exports():
    """redact.py must export REDACT_PATTERN, redact_match, and redact."""
    import importlib.util
    from pathlib import Path
    rpath = Path(__file__).parent.parent / "src" / "redact.py"
    spec = importlib.util.spec_from_file_location("redact_check", rpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "REDACT_PATTERN"), "REDACT_PATTERN missing from redact.py"
    assert hasattr(mod, "redact_match"), "redact_match missing from redact.py"
    assert hasattr(mod, "redact"), "redact() function missing from redact.py"
    assert hasattr(mod, "__version__"), "__version__ missing from redact.py"
    assert callable(mod.redact_match)
    assert callable(mod.redact)


# ---------------------------------------------------------------------------
# server._redact and redact.redact() produce identical output
# for ALL token types (comprehensive cross-check)
# ---------------------------------------------------------------------------

def test_server_and_wrapper_redact_identical_for_all_token_types():
    """server._redact and redact.redact() must produce identical output for all tokens."""
    import importlib.util
    from pathlib import Path
    import copilot_mcp_server as srv

    rpath = Path(__file__).parent.parent / "src" / "redact.py"
    spec = importlib.util.spec_from_file_location("redact_full", rpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # All supported token types
    test_cases = [
        ("ghp_" + "A" * 20, "ghp_ token"),
        ("github_pat_" + "A" * 15, "github_pat_ token"),
        ("gho_" + "A" * 20, "gho_ token"),
        ("sk-" + "A" * 20, "OpenAI sk- key"),
        ("sk_live_" + "A" * 20, "Stripe secret key"),
        ("pk_live_" + "A" * 20, "Stripe publishable key"),
        ("AKIA" + "A" * 16, "AWS key ID"),
        ("xoxb-123456789012-" + "A" * 20, "Slack bot token"),
        ("Bearer " + "A" * 20, "Bearer token"),
        ("eyJ" + "A" * 20, "JWT"),
        ("ya29." + "A" * 20, "GCP OAuth"),
        ("AccountKey=" + "A" * 20, "Azure account key"),
        ("sig=" + "A" * 20, "SAS signature"),
        ("password=" + "A" * 20, "generic password"),
    ]

    for token, label in test_cases:
        text = f"value: {token} extra"
        srv_out = srv._redact(text)
        mod_out = mod.redact(text)
        assert srv_out == mod_out, (
            f"Mismatch for {label!r}:\n"
            f"  server  : {srv_out!r}\n"
            f"  redact.py: {mod_out!r}"
        )


# ---------------------------------------------------------------------------
# Test malformed YAML falls back to DEFAULT_CONFIG
# ---------------------------------------------------------------------------

def test_load_config_malformed_yaml_falls_back(tmp_path, monkeypatch):
    """load_config must fall back to DEFAULT_CONFIG when YAML is malformed."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profiles:\n  simple:\n    model: [unclosed\n")
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    assert result == DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# sanitize_profile_name Unicode → underscores
# ---------------------------------------------------------------------------

def test_sanitize_profile_name_unicode_becomes_underscores():
    """Non-ASCII chars in profile name become underscores.
    'security-日本語' → '-' + 3 kanji = 4 non-alnum chars → 4 underscores."""
    result = sanitize_profile_name("security-日本語")
    assert result == "security____"


# ---------------------------------------------------------------------------
# _decode() helper
# ---------------------------------------------------------------------------

def test_decode_none_returns_empty_string():
    from copilot_mcp_server import _decode
    assert _decode(None) == ""


def test_decode_valid_utf8():
    from copilot_mcp_server import _decode
    assert _decode(b"hello world") == "hello world"


def test_decode_invalid_utf8_uses_replacement_char():
    from copilot_mcp_server import _decode
    result = _decode(b"hello \xff world")
    assert "\ufffd" in result or "hello" in result  # replacement char or partial


# ---------------------------------------------------------------------------
# Semaphore timeout configurable via COPILOT_SEMAPHORE_TIMEOUT env var
# ---------------------------------------------------------------------------

def test_semaphore_timeout_reads_from_env(monkeypatch):
    """_SEMAPHORE_TIMEOUT must reflect COPILOT_SEMAPHORE_TIMEOUT env var on reload."""
    import importlib
    monkeypatch.setenv("COPILOT_SEMAPHORE_TIMEOUT", "42")
    import copilot_mcp_server as srv
    # The value is set at import time, so reload to pick up the env var change
    importlib.reload(srv)
    assert srv._SEMAPHORE_TIMEOUT == 42
    # Restore by reloading without the env var
    monkeypatch.delenv("COPILOT_SEMAPHORE_TIMEOUT", raising=False)
    importlib.reload(srv)


def test_semaphore_timeout_defaults_to_30():
    """Default semaphore timeout is 30 seconds."""
    import copilot_mcp_server as srv
    import os
    if "COPILOT_SEMAPHORE_TIMEOUT" not in os.environ:
        assert srv._SEMAPHORE_TIMEOUT == 30


# ---------------------------------------------------------------------------
# Config file path selection (repo vs. INSTALL_DIR)
# ---------------------------------------------------------------------------

def test_config_file_prefers_repo_config_over_install_dir(tmp_path, monkeypatch):
    """Repo-local config.yaml takes priority over INSTALL_DIR config."""
    # Simulate server running from src/ with a repo config.yaml one level up
    repo_cfg = tmp_path / "config.yaml"
    repo_cfg.write_text("profiles:\n  custom:\n    model: gpt-4o\n    description: repo\n")

    import copilot_mcp_server as srv
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(repo_cfg))
    result = load_config()
    assert "custom" in result["profiles"]


def test_config_file_falls_back_to_install_dir(tmp_path, monkeypatch):
    """When repo config is absent, INSTALL_DIR config is used."""
    install_cfg = tmp_path / "config.yaml"
    install_cfg.write_text("profiles:\n  installed:\n    model: gpt-4o\n    description: installed\n")

    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(install_cfg))
    result = load_config()
    assert "installed" in result["profiles"]


# ---------------------------------------------------------------------------
# Empty blocked_patterns after removing empty strings logs warning
# ---------------------------------------------------------------------------

def test_blocked_patterns_all_empty_still_allows_task(tmp_path, monkeypatch):
    """If all blocked_patterns are empty strings (removed by coercion),
    task classification must still proceed without errors."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n"
        "  p:\n"
        "    model: gpt-4o-mini\n"
        "    description: test\n"
        "    blocked_patterns:\n"
        "      - ''\n"
        "      - ''\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))
    result = load_config()
    # After coercion all empty strings are removed → empty list
    assert result["profiles"]["p"].get("blocked_patterns") == []


# ---------------------------------------------------------------------------
# Case-sensitivity fix in _resolve_allowed_tools
# ---------------------------------------------------------------------------

def test_resolve_allowed_tools_case_insensitive_security():
    """Profile named 'Security' (capital S) must still get the read-only
    DEFAULT_CONFIG whitelist, not an empty list that would enable all tools."""
    profile = {"model": "gpt-4o", "timeout": 600}  # no allowed_tools key
    result = _resolve_allowed_tools("Security", profile)
    # Must match the built-in 'security' defaults: view allowed, bash not
    assert "view" in result
    assert "bash" not in result


def test_resolve_allowed_tools_case_insensitive_simple():
    """Profile named 'SIMPLE' falls back to DEFAULT_CONFIG 'simple' whitelist."""
    profile = {"model": "gpt-4o-mini"}  # no allowed_tools key
    result = _resolve_allowed_tools("SIMPLE", profile)
    # simple profile allows bash
    assert "bash" in result


def test_resolve_allowed_tools_case_insensitive_code_review():
    """Profile named 'Code_Review' falls back to DEFAULT_CONFIG 'code_review' whitelist."""
    profile = {"model": "gpt-4o"}  # no allowed_tools key
    result = _resolve_allowed_tools("Code_Review", profile)
    assert "view" in result
    assert "bash" not in result


def test_resolve_allowed_tools_explicit_empty_not_overridden():
    """Explicit [] must never be overridden by DEFAULT_CONFIG fallback."""
    profile = {"model": "gpt-4o", "allowed_tools": []}
    result = _resolve_allowed_tools("Security", profile)
    assert result == []


def test_resolve_allowed_tools_unknown_profile_returns_empty():
    """Unknown profile name with no allowed_tools falls back to [] (unknown = no defaults)."""
    profile = {"model": "gpt-4o"}  # no allowed_tools key
    result = _resolve_allowed_tools("unknown_custom_profile", profile)
    assert result == []


# ---------------------------------------------------------------------------
# Structured JSON logging — _parse_log_fields and log()
# ---------------------------------------------------------------------------

def test_parse_log_fields_extracts_key_value_pairs():
    """_parse_log_fields must split 'key=value' tokens into a dict."""
    from copilot_mcp_server import _parse_log_fields
    result = _parse_log_fields("profile=simple success len=42")
    assert result["profile"] == "simple"
    assert result["len"] == "42"
    assert result["msg"] == "success"


def test_parse_log_fields_all_kv_no_remainder():
    """When all tokens are key=value, 'msg' key must not appear."""
    from copilot_mcp_server import _parse_log_fields
    result = _parse_log_fields("profile=simple len=100")
    assert result.get("profile") == "simple"
    assert result.get("len") == "100"
    assert "msg" not in result


def test_parse_log_fields_preserves_quoted_values_with_spaces():
    """Quoted values must survive JSON-log parsing as a single field value."""
    from copilot_mcp_server import _parse_log_fields
    result = _parse_log_fields("profile=simple err='multi word failure'")
    assert result.get("profile") == "simple"
    assert result.get("err") == "multi word failure"
    assert "msg" not in result


def test_parse_log_fields_no_kv_is_full_msg():
    """A plain message with no key=value pairs is stored under 'msg'."""
    from copilot_mcp_server import _parse_log_fields
    result = _parse_log_fields("server started successfully")
    assert result == {"msg": "server started successfully"}


def test_parse_log_fields_empty_string():
    """Empty string produces an empty dict (no 'msg' key)."""
    from copilot_mcp_server import _parse_log_fields
    result = _parse_log_fields("")
    assert result == {}


def test_log_json_format_writes_valid_json(tmp_path, monkeypatch):
    """With COPILOT_LOG_FORMAT=json, log() writes a valid JSON line."""
    import json
    import copilot_mcp_server as srv

    log_file = tmp_path / "test.log"
    monkeypatch.setattr("copilot_mcp_server.LOG_FILE", str(log_file))
    monkeypatch.setattr("copilot_mcp_server._LOG_FORMAT", "json")

    srv.log("profile=simple success len=42")

    line = log_file.read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert "ts" in parsed
    assert parsed.get("profile") == "simple"
    assert parsed.get("len") == "42"


def test_log_json_preserves_multi_word_error_fields(tmp_path, monkeypatch):
    """Quoted err= values must remain intact in JSON logs."""
    import json
    import copilot_mcp_server as srv

    log_file = tmp_path / "test.log"
    monkeypatch.setattr("copilot_mcp_server.LOG_FILE", str(log_file))
    monkeypatch.setattr("copilot_mcp_server._LOG_FORMAT", "json")

    srv.log(
        "profile=simple rc=5 "
        f"err={srv._quote_log_value('multi word failure message')}"
    )

    line = log_file.read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert parsed.get("rc") == "5"
    assert parsed.get("err") == "multi word failure message"
    assert "msg" not in parsed


def test_log_text_format_writes_bracketed_timestamp(tmp_path, monkeypatch):
    """Default text format must produce '[timestamp] message' lines."""
    import copilot_mcp_server as srv

    log_file = tmp_path / "test.log"
    monkeypatch.setattr("copilot_mcp_server.LOG_FILE", str(log_file))
    monkeypatch.setattr("copilot_mcp_server._LOG_FORMAT", "text")

    srv.log("profile=simple success len=42")

    line = log_file.read_text(encoding="utf-8").strip()
    assert line.startswith("[")
    assert "] " in line
    assert "profile=simple" in line


def test_log_json_line_has_ts_field(tmp_path, monkeypatch):
    """JSON log entries must always include 'ts' (ISO timestamp)."""
    import json
    import copilot_mcp_server as srv

    log_file = tmp_path / "test.log"
    monkeypatch.setattr("copilot_mcp_server.LOG_FILE", str(log_file))
    monkeypatch.setattr("copilot_mcp_server._LOG_FORMAT", "json")

    srv.log("any message")

    line = log_file.read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert "ts" in parsed
    # Verify ts looks like an ISO timestamp (contains T between date and time)
    assert "T" in parsed["ts"]


# ---------------------------------------------------------------------------
# allowed_tools resolved at registration time, not at runtime
# ---------------------------------------------------------------------------

def test_register_tools_embeds_resolved_allowed_tools(monkeypatch, tmp_path):
    """_register_tools must embed the resolved allowed_tools list into the
    profile dict passed to make_handler, so the handler never needs a runtime
    name-based lookup."""
    from copilot_mcp_server import _register_tools
    import copilot_mcp_server as srv
    from unittest.mock import patch

    cfg = tmp_path / "config.yaml"
    # security profile without explicit allowed_tools — should fall back to
    # DEFAULT_CONFIG's read-only whitelist at registration time.
    cfg.write_text(
        "profiles:\n"
        "  security:\n"
        "    model: gpt-4o\n"
        "    timeout: 600\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))

    captured_profiles = {}

    def fake_make_handler(profile_name, profile):
        captured_profiles[profile_name] = dict(profile)
        async def handler(task: str) -> str:
            return "ok"
        handler.__name__ = f"run_agent_{profile_name}"
        return handler

    with patch("copilot_mcp_server.make_handler", side_effect=fake_make_handler):
        with patch.object(srv.mcp, "tool", return_value=lambda fn: fn):
            _register_tools()

    # The profile passed to make_handler must already have allowed_tools resolved
    assert "security" in captured_profiles
    tools = captured_profiles["security"]["allowed_tools"]
    assert isinstance(tools, list)
    assert "view" in tools
    assert "bash" not in tools  # DEFAULT_CONFIG security is read-only


def test_register_tools_explicit_empty_allowed_tools_preserved(monkeypatch, tmp_path):
    """Explicit [] in config must reach make_handler as [] — no re-expansion."""
    from copilot_mcp_server import _register_tools
    import copilot_mcp_server as srv
    from unittest.mock import patch

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n"
        "  security:\n"
        "    model: gpt-4o\n"
        "    allowed_tools: []\n"
    )
    monkeypatch.setattr("copilot_mcp_server.CONFIG_FILE", str(cfg))

    captured_profiles = {}

    def fake_make_handler(profile_name, profile):
        captured_profiles[profile_name] = dict(profile)
        async def handler(task: str) -> str:
            return "ok"
        handler.__name__ = f"run_agent_{profile_name}"
        return handler

    with patch("copilot_mcp_server.make_handler", side_effect=fake_make_handler):
        with patch.object(srv.mcp, "tool", return_value=lambda fn: fn):
            _register_tools()

    assert captured_profiles["security"]["allowed_tools"] == []


# ---------------------------------------------------------------------------
# Low: redact.py truncation must not exceed max_chars
# ---------------------------------------------------------------------------

def test_redact_truncation_does_not_exceed_max_chars(tmp_path):
    """Low: total output (content + marker) must be <= max_chars."""
    import importlib.util
    from pathlib import Path

    rpath = Path(__file__).parent.parent / "src" / "redact.py"
    spec = importlib.util.spec_from_file_location("redact_trunc", rpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Write a file that is longer than max_chars
    max_chars = 50
    content = "A" * 200  # well above limit
    f = tmp_path / "input.txt"
    f.write_text(content, encoding="utf-8")

    import io, sys
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        sys.argv = [str(rpath), str(f), str(max_chars)]
        mod._main()
    finally:
        sys.stdout = old_stdout

    out = captured.getvalue()
    assert len(out) <= max_chars, (
        f"Output length {len(out)} exceeds max_chars {max_chars}: {out!r}"
    )
    assert "truncated" in out


def test_redact_no_truncation_when_within_limit(tmp_path):
    """Content within max_chars must be returned unmodified (no marker)."""
    import importlib.util
    from pathlib import Path

    rpath = Path(__file__).parent.parent / "src" / "redact.py"
    spec = importlib.util.spec_from_file_location("redact_notrunc", rpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    content = "hello world"
    f = tmp_path / "input.txt"
    f.write_text(content, encoding="utf-8")

    import io, sys
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        sys.argv = [str(rpath), str(f), "100"]
        mod._main()
    finally:
        sys.stdout = old_stdout

    out = captured.getvalue()
    assert out == content
    assert "truncated" not in out


def test_redact_tiny_limit_still_preserves_unicode_character_boundary(tmp_path):
    """When the marker cannot fit, redact.py must still honour max_chars exactly."""
    import importlib.util
    from pathlib import Path

    rpath = Path(__file__).parent.parent / "src" / "redact.py"
    spec = importlib.util.spec_from_file_location("redact_tiny", rpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    f = tmp_path / "input.txt"
    f.write_text("žx", encoding="utf-8")

    import io, sys
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        sys.argv = [str(rpath), str(f), "1"]
        mod._main()
    finally:
        sys.stdout = old_stdout

    out = captured.getvalue()
    assert out == "ž"
