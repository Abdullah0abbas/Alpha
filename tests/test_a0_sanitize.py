"""
Offline unit tests for Phase A0-4 sanitization and masking utilities (bot/utils/sanitize.py).
Requires zero network access and zero database connections.
"""
from bot.utils.sanitize import mask_details, mask_value, sanitize_exception_message


def test_sanitize_rpc_url_strips_path_query_and_credentials():
    raw_exc = RuntimeError(
        "HTTPSConnectionPool: Max retries exceeded with url: "
        "https://bsc-mainnet.nodereal.io/v1/64a8f9c2b1e04d3a9f8c7b6a5e4d3c2b?api_key=secret123"
    )
    cleaned = sanitize_exception_message(raw_exc)
    assert "https://bsc-mainnet.nodereal.io" in cleaned
    assert "64a8f9c2b1e04d3a9f8c7b6a5e4d3c2b" not in cleaned
    assert "secret123" not in cleaned
    assert "/v1/" not in cleaned


def test_sanitize_url_with_basic_auth_and_port():
    raw_exc = "Connection refused to http://admin:super_secret_pw@rpc.internal.node:8545/bsc/private"
    cleaned = sanitize_exception_message(raw_exc)
    assert cleaned == "Connection refused to http://rpc.internal.node:8545"
    assert "super_secret_pw" not in cleaned
    assert "/bsc/private" not in cleaned


def test_sanitize_64_char_hex_with_and_without_0x():
    raw_hex = "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
    prefixed_hex = f"0x{raw_hex}"

    msg = f"Failed signing with raw={raw_hex} and prefixed={prefixed_hex}."
    cleaned = sanitize_exception_message(msg)

    assert raw_hex not in cleaned
    assert prefixed_hex not in cleaned
    assert "4c0883…2318" in cleaned
    assert "0x4c08…2318" in cleaned


def test_sanitize_mnemonic_12_and_24_words():
    mnemonic_12 = "abandon ability able about above absent absorb abstract absurd abuse access accident"
    mnemonic_24 = (
        "abandon ability able about above absent absorb abstract absurd abuse access accident "
        "account accuse achieve acid acoustic acquire across act action actor actress actual"
    )

    cleaned_12 = sanitize_exception_message(f"Wallet init error: {mnemonic_12}")
    assert cleaned_12 == "Wallet init error: [REDACTED_MNEMONIC]"
    assert "abandon" not in cleaned_12

    cleaned_24 = sanitize_exception_message(f"Seed phrase invalid ({mnemonic_24})")
    assert cleaned_24 == "Seed phrase invalid ([REDACTED_MNEMONIC])"
    assert "actress" not in cleaned_24


def test_sanitize_cuts_sql_and_parameters():
    sql_exc = (
        " (sqlalchemy.dialects.postgresql.asyncpg.IntegrityError) duplicate key value "
        "[SQL: INSERT INTO users (telegram_id) VALUES ($1)] [parameters: (123456,)] "
        "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
    )
    cleaned = sanitize_exception_message(sql_exc)
    assert cleaned == "(sqlalchemy.dialects.postgresql.asyncpg.IntegrityError) duplicate key value"
    assert "[SQL:" not in cleaned
    assert "[parameters:" not in cleaned


def test_normal_messages_remain_unchanged():
    en_msg = "Deal #42 is already in COMPLETED state and cannot be cancelled."
    ar_msg = "تعذر إتمام العملية: الصفقة رقم #105 ملغاة مسبقاً."

    assert sanitize_exception_message(en_msg) == en_msg
    assert sanitize_exception_message(ar_msg) == ar_msg


def test_mask_details_recursive_and_non_mutating():
    raw_hex = "9f8c7b6a5e4d3c2b64a8f9c2b1e04d3a9f8c7b6a5e4d3c2b64a8f9c2b1e04d3a"
    mnemonic_12 = "abandon ability able about above absent absorb abstract absurd abuse access accident"
    original = {
        "buyer_payout_address": "0x71C7656EC7ab88b098defB751B7401B5f6d8976F",
        "rpc_endpoint": "https://rpc.ankr.com/bsc/my_secret_rpc_token_987654",
        "tx_key": raw_hex,
        "prefixed_key": f"0x{raw_hex}",
        "nested": {
            "seed_phrase": mnemonic_12,
            "note": f"Error on {mnemonic_12}",
        },
        "status": "COMPLETED",
    }

    masked = mask_details(original)

    # Original must not be mutated
    assert original["rpc_endpoint"].endswith("my_secret_rpc_token_987654")
    assert original["tx_key"] == raw_hex

    # Masked copy checks
    assert masked["buyer_payout_address"] == "0x71C7…976F"
    assert masked["rpc_endpoint"] == "https://rpc.ankr.com"
    assert masked["tx_key"] == "9f8c7b…4d3a"
    assert masked["prefixed_key"] == "0x9f8c…4d3a"
    assert masked["nested"]["seed_phrase"] == "[REDACTED_MNEMONIC]"
    assert masked["nested"]["note"] == "Error on [REDACTED_MNEMONIC]"
    assert masked["status"] == "COMPLETED"
    assert mask_value(None) is None
