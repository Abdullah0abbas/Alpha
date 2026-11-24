"""
Pure sanitization and masking utilities for Alpha Bot.
Contains zero database or Telegram dependencies to enable fast, deterministic unit testing.
"""
import re
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

# Matches http:// or https:// URLs up to whitespace or closing delimiter/quote
_URL_RE = re.compile(r"https?://[^\s'\"<>)\]]+", re.IGNORECASE)

# Matches 64-character hexadecimal strings with or without a leading 0x/0X prefix
_HEX64_RE = re.compile(r"(?<![0-9a-fA-FxX])(?:0[xX])?[0-9a-fA-F]{64}(?![0-9a-fA-Za-z_])")

# Matches 12 to 24 consecutive lowercase English words (3-8 letters, standard BIP-39 word lengths)
_MNEMONIC_RE = re.compile(r"(?<![A-Za-z0-9_-])(?:[a-z]{3,8}\s+){11,23}[a-z]{3,8}(?![A-Za-z0-9_-])")


def _mask_url_match(match: re.Match) -> str:
    raw_url = match.group(0)
    trailing = ""
    while raw_url and raw_url[-1] in ".,;:!?":
        trailing = raw_url[-1] + trailing
        raw_url = raw_url[:-1]

    try:
        parsed = urlsplit(raw_url)
        scheme = (parsed.scheme or "https").lower()
        host = parsed.hostname or ""
        try:
            port = f":{parsed.port}" if parsed.port else ""
        except ValueError:
            port = ""
        if host:
            return f"{scheme}://{host}{port}{trailing}"
    except Exception:
        pass
    return f"https://[REDACTED_URL]{trailing}"


def _mask_hex64_match(match: re.Match) -> str:
    token = match.group(0)
    return f"{token[:6]}…{token[-4:]}"


def _sanitize_sensitive_patterns(text: str) -> str:
    """
    Scans a text string and masks:
    1. Any http(s):// URL -> keeps only scheme://hostname(:port), stripping userinfo, path, query, fragment.
    2. Any 64-hex string (with or without 0x prefix) -> shortened to first 6 and last 4 characters.
    3. Any 12..24 consecutive lowercase English words (mnemonic) -> '[REDACTED_MNEMONIC]'.
    """
    if not text:
        return text
    out = _URL_RE.sub(_mask_url_match, text)
    out = _HEX64_RE.sub(_mask_hex64_match, out)
    out = _MNEMONIC_RE.sub("[REDACTED_MNEMONIC]", out)
    return out


def mask_value(value: Any) -> Any:
    """
    Shortens long sensitive values (addresses, IBANs, phone numbers):
    Keeps first 6 and last 4 characters and inserts '…' between them if length > 14.
    Otherwise returns the value as-is.
    """
    if value is None:
        return None
    val_str = _sanitize_sensitive_patterns(str(value))
    if val_str == "[REDACTED_MNEMONIC]":
        return val_str
    if len(val_str) > 14:
        return f"{val_str[:6]}…{val_str[-4:]}"
    return value if val_str == str(value) else val_str


def _sanitize_any_value(val: Any) -> Any:
    if isinstance(val, str):
        return _sanitize_sensitive_patterns(val)
    if isinstance(val, list):
        return [_sanitize_any_value(item) for item in val]
    if isinstance(val, tuple):
        return tuple(_sanitize_any_value(item) for item in val)
    return val


def mask_details(details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Returns a copy of the dictionary where:
    - Values for keys containing (case-insensitively) any of:
      'address', 'wallet', 'info', 'payment', 'mnemonic', 'seed', 'private', 'secret'
      are masked using mask_value.
    - All string values (under any key, including nested structures) are scanned to redact
      http(s):// URL paths/keys, 64-char hex strings (with or without 0x), and 12/24-word mnemonics.
    Works recursively on nested dictionaries without mutating the original dictionary.
    """
    if not isinstance(details, dict):
        return {} if details is None else details

    sensitive_patterns = (
        "address",
        "wallet",
        "info",
        "payment",
        "mnemonic",
        "seed",
        "private",
        "secret",
    )
    result: Dict[str, Any] = {}

    for k, v in details.items():
        k_lower = str(k).lower()
        if isinstance(v, dict):
            result[k] = mask_details(v)
        elif any(pat in k_lower for pat in sensitive_patterns):
            result[k] = mask_value(v)
        else:
            result[k] = _sanitize_any_value(v)

    return result


def sanitize_exception_message(exc: Any, max_len: int = 200) -> str:
    """
    Returns exception string:
    1. Cut at the first occurrence of '[SQL:', '[parameters:', or '(Background on this error'.
    2. Collapsed to single-line whitespace.
    3. Stripped of URL paths/queries/credentials (keeping main host only),
       64-char hex strings (with or without 0x, shortened to first 6 and last 4 chars),
       and 12..24 word mnemonics.
    4. Truncated to max_len.
    """
    if exc is None:
        return ""
    msg = str(exc)
    cut_markers = ["[SQL:", "[parameters:", "(Background on this error"]
    cut_idx = len(msg)
    for marker in cut_markers:
        idx = msg.find(marker)
        if idx != -1 and idx < cut_idx:
            cut_idx = idx

    cleaned = msg[:cut_idx].strip()
    cleaned = " ".join(cleaned.split())
    cleaned = _sanitize_sensitive_patterns(cleaned)
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned

