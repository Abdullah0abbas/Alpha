"""
Pure sanitization and masking utilities for Alpha Bot.
Contains zero database or Telegram dependencies to enable fast, deterministic unit testing.
"""
from typing import Any, Dict, Optional


def mask_value(value: Any) -> Any:
    """
    Shortens long sensitive values (addresses, IBANs, phone numbers):
    Keeps first 6 and last 4 characters and inserts '…' between them if length > 14.
    Otherwise returns the value as-is.
    """
    if value is None:
        return None
    val_str = str(value)
    if len(val_str) > 14:
        return f"{val_str[:6]}…{val_str[-4:]}"
    return value


def mask_details(details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Returns a copy of the dictionary where values for keys containing
    (case-insensitively) any of: 'address', 'wallet', 'info', 'payment'
    are masked using mask_value.
    Works recursively on nested dictionaries without mutating the original dictionary.
    """
    if not isinstance(details, dict):
        return {} if details is None else details

    sensitive_patterns = ("address", "wallet", "info", "payment")
    result: Dict[str, Any] = {}

    for k, v in details.items():
        k_lower = str(k).lower()
        if isinstance(v, dict):
            result[k] = mask_details(v)
        elif any(pat in k_lower for pat in sensitive_patterns):
            result[k] = mask_value(v)
        else:
            result[k] = v

    return result


def sanitize_exception_message(exc: Any, max_len: int = 200) -> str:
    """
    Returns exception string cut at the first occurrence of:
    '[SQL:', '[parameters:', or '(Background on this error',
    truncated to max_len, and without newlines.
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
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned
