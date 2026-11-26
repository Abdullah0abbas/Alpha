"""Offline unit tests for bot.utils.sanitize (Phase A0-4)."""
from copy import deepcopy

from bot.utils.sanitize import mask_details, mask_value, sanitize_exception_message


def test_sanitize_exception_strips_url_credentials_path_and_query():
    exc = RuntimeError(
        "RPC error from https://user:secretpass@bsc-mainnet.nodereal.io:8545/v1/abc123SECRET?api_key=topsecret#frag!"
    )
    cleaned = sanitize_exception_message(exc)
    assert "https://bsc-mainnet.nodereal.io:8545!" in cleaned
    assert "secretpass" not in cleaned
    assert "abc123SECRET" not in cleaned
    assert "topsecret" not in cleaned
    assert "frag" not in cleaned


def test_sanitize_exception_masks_64_hex_with_and_without_0x():
    hex_plain = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e9988"
    hex_prefixed = "0x" + hex_plain

    cleaned_plain = sanitize_exception_message(f"key={hex_plain}")
    assert hex_plain not in cleaned_plain
    assert "a1b2c3…9988" in cleaned_plain

    cleaned_prefixed = sanitize_exception_message(f"tx={hex_prefixed}")
    assert hex_prefixed not in cleaned_prefixed
    assert "0xa1b2…9988" in cleaned_prefixed


def test_sanitize_exception_redacts_12_and_24_word_mnemonics():
    mnemonic_12 = (
        "abandon ability able about above absent absorb abstract absurd abuse access accident"
    )
    mnemonic_24 = f"{mnemonic_12} {mnemonic_12}"

    cleaned_12 = sanitize_exception_message(f"seed leak: {mnemonic_12} end")
    assert "[REDACTED_MNEMONIC]" in cleaned_12
    assert "abandon" not in cleaned_12

    cleaned_24 = sanitize_exception_message(f"seed leak: {mnemonic_24} end")
    assert "[REDACTED_MNEMONIC]" in cleaned_24
    assert "accident" not in cleaned_24


def test_sanitize_exception_cuts_sql_markers_and_truncates():
    raw = (
        "IntegrityError: duplicate key value\n\n"
        "[SQL: INSERT INTO users VALUES (%s)] [parameters: ('secret',)] "
        "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
    )
    cleaned = sanitize_exception_message(raw, max_len=200)
    assert cleaned == "IntegrityError: duplicate key value"
    assert "[SQL:" not in cleaned
    assert "secret" not in cleaned

    truncated = sanitize_exception_message("x" * 300, max_len=50)
    assert len(truncated) == 50
    assert sanitize_exception_message(None) == ""


def test_mask_value_behavior():
    assert mask_value(None) is None
    assert mask_value("short_val") == "short_val"
    assert mask_value("0x1234567890abcdef1234567890abcdef12345678") == "0x1234…5678"

    mnemonic_12 = (
        "abandon ability able about above absent absorb abstract absurd abuse access accident"
    )
    assert mask_value(mnemonic_12) == "[REDACTED_MNEMONIC]"


def test_mask_details_recursive_and_non_mutating():
    hex_key = "0x" + "f" * 60 + "1234"
    mnemonic_12 = (
        "abandon ability able about above absent absorb abstract absurd abuse access accident"
    )
    original = {
        "deal_id": 42,
        "wallet_address": "0x1111222233334444555566667777888899990000",
        "SECRET_TOKEN": "super_secret_long_token_value",
        "nested": {
            "payment_info": "shamcash_0123456789abcdef01234567",
            "rpc_url": "https://rpc.example.com/v2/my_private_api_key",
            "note": f"leaked {hex_key}",
            "items": [mnemonic_12, "ok"],
        },
    }
    snapshot = deepcopy(original)

    masked = mask_details(original)

    # Original dictionary must never be mutated
    assert original == snapshot

    assert masked["deal_id"] == 42
    assert masked["wallet_address"] == "0x1111…0000"
    assert masked["SECRET_TOKEN"] == "super_…alue"
    assert masked["nested"]["payment_info"] == "shamca…4567"
    assert masked["nested"]["rpc_url"] == "https://rpc.example.com"
    assert hex_key not in masked["nested"]["note"]
    assert "0xffff…1234" in masked["nested"]["note"]
    assert masked["nested"]["items"] == ["[REDACTED_MNEMONIC]", "ok"]
    assert mask_details(None) == {}
