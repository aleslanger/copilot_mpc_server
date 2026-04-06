#!/usr/bin/env python3
"""Shared secret-redaction logic used by both the MCP server and the wrapper.

This is the single source of truth for the redaction regex.  Both consumers
reference this file so any future pattern change automatically propagates to
both the server log path and the wrapper output path without manual syncing.

Standalone usage (called by copilot_wrapper.sh):
    python3 redact.py <input-file> <max-output-chars>

Imported usage (copilot_mcp_server.py):
    from redact import REDACT_PATTERN, redact_match
"""
import re
import sys

# Version exported so the importer (copilot_mcp_server.py) can verify the
# module interface hasn't changed after an incomplete update.
__version__ = "1.0"
_REQUIRED_EXPORTS = ("REDACT_PATTERN", "redact_match", "redact")

# ---------------------------------------------------------------------------
# Canonical redaction pattern — add new token types here.
# NOTE: Both consumers use this same pattern.  Do NOT add patterns to only
# one of the two callers — always add them here.
# ---------------------------------------------------------------------------
REDACT_PATTERN = re.compile(
    r"(ghp_[A-Za-z0-9]{10,}"              # GitHub classic PAT
    r"|github_pat_[A-Za-z0-9_]{10,}"       # GitHub fine-grained PAT
    r"|gho_[A-Za-z0-9]{10,}"              # GitHub OAuth token
    r"|sk-[A-Za-z0-9]{10,}"               # OpenAI secret key
    r"|sk_live_[A-Za-z0-9]{10,}"          # Stripe secret key
    r"|pk_live_[A-Za-z0-9]{10,}"          # Stripe publishable key
    r"|AKIA[0-9A-Z]{10,}"                 # AWS access key ID
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"     # Slack tokens
    # Bearer\s+ covers both space and tab separators
    r"|Bearer\s+[A-Za-z0-9._~+/=-]{10,}"  # HTTP Bearer token
    r"|eyJ[A-Za-z0-9._-]{10,}"            # JWT / GCP service-account token
    r"|ya29\.[A-Za-z0-9._-]{10,}"         # GCP OAuth access token
    r"|AIzaSy[A-Za-z0-9_-]{33}"           # Google API key
    r"|glpat-[A-Za-z0-9_-]{20,}"          # GitLab PAT
    r"|gldt-[A-Za-z0-9_-]{20,}"           # GitLab deploy token
    r"|SG\.[A-Za-z0-9_-]{22,}"            # SendGrid API key
    r"|SK[a-f0-9]{32}"                    # Twilio-like key
    r"|sbp_[A-Za-z0-9]{40,}"              # Supabase key
    r"|sk-ant-[A-Za-z0-9_-]{90,}"         # Anthropic key
    r"|AccountKey=[A-Za-z0-9+/]{10,}=*"   # Azure Storage account key
    r"|sig=[A-Za-z0-9%+/]{10,}"           # Azure SAS / HMAC signature
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|-----BEGIN CERTIFICATE-----"  # PEM headers
    r"|(?:postgresql|mysql|mongodb)://[^@\s]{3,}@\S+"  # DB URLs with embedded creds
    # Negative lookahead: do NOT redact when the RHS looks like a function/method
    # call (contains '(').  This prevents corrupting legitimate code examples like
    #   password=input("Enter password: ")
    #   token=request.headers.get("Authorization")
    r"|(?:api[_-]?key|token|secret|password|passwd)=(?![^\s,;'\"(\[{]*\()[^\s,;'\"(\[{]+)",
    re.IGNORECASE | re.DOTALL,
)


def redact_match(m: re.Match) -> str:
    """Replacement function for REDACT_PATTERN.sub()."""
    s = m.group(0)
    # key=value patterns: preserve the key name for context
    if "=" in s and not s.startswith("---") and "://" not in s:
        k, _ = s.split("=", 1)
        return f"{k}=REDACTED"
    lower = s.lower()
    if lower.startswith("sk_live_"):    return "sk_live_REDACTED"
    if lower.startswith("pk_live_"):    return "pk_live_REDACTED"
    if lower.startswith("sk-"):         return "sk-REDACTED"
    if lower.startswith("ghp_"):        return "ghp_REDACTED"
    if lower.startswith("github_pat_"): return "github_pat_REDACTED"
    if lower.startswith("gho_"):        return "gho_REDACTED"
    if s.startswith("AKIA"):            return "AKIA_REDACTED"
    if lower.startswith("xox"):         return "xox_REDACTED"
    if s.startswith("eyJ"):             return "eyJ_REDACTED"
    if s.startswith("ya29."):           return "ya29.REDACTED"
    if lower.startswith("bearer"):      return "Bearer REDACTED"
    if s.startswith("---"):             return "[REDACTED-PEM]"
    if "://" in s:                      return "[REDACTED-URL]"
    return "[REDACTED]"


def redact(text: str) -> str:
    """Return *text* with all recognised secret patterns replaced."""
    return REDACT_PATTERN.sub(redact_match, text)


# ---------------------------------------------------------------------------
# Standalone mode: redact <file> and write at most <max_chars> to stdout.
# Called by copilot_wrapper.sh after Copilot produces its output.
# ---------------------------------------------------------------------------
def _main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input-file> <max-output-chars>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    try:
        max_chars = int(sys.argv[2])
        if max_chars <= 0:
            raise ValueError
    except ValueError:
        print("ERROR: max-output-chars must be a positive integer", file=sys.stderr)
        sys.exit(1)

    # Read the entire file at once so no token can straddle a chunk boundary.
    # Files are bounded by the Copilot timeout and max_output_length (typically < 30 KB).
    with open(path, encoding="utf-8", errors="replace") as fh:
        content = fh.read()

    redacted = REDACT_PATTERN.sub(redact_match, content)

    if len(redacted) > max_chars:
        # The truncation marker is part of the output, so it counts toward
        # max_chars.  Trim the content so that content + marker <= max_chars,
        # honouring the documented contract that output never exceeds the limit.
        # If max_chars is smaller than the marker itself, return only the
        # truncated content prefix; there is no room to add a marker.
        _MARKER = "\n...[truncated]\n"
        if max_chars <= len(_MARKER):
            sys.stdout.write(redacted[:max_chars])
        else:
            cut = max_chars - len(_MARKER)
            sys.stdout.write(redacted[:cut])
            sys.stdout.write(_MARKER)
    else:
        sys.stdout.write(redacted)


if __name__ == "__main__":
    _main()
