"""Offline unit tests for encrypted backups and restore validation (Phase A0-1, A0-2, A0-5)."""
import gzip
import json
import os
import stat
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

from bot.services import backup_service
from db.models import BlockchainTransaction, Deal
from scripts import restore_backup


def _sample_backup_payload(version: str = "4.1") -> dict:
    return {
        "metadata": {
            "created_at": "2026-11-24T00:00:00+00:00",
            "version": version,
            "table_stats": {"users": 1, "offers": 0, "deals": 0},
            "deposit_index_seq": {"last_value": 5, "is_called": True},
        },
        "users": [
            {
                "telegram_id": 1001,
                "username": "trader_alpha",
                "nickname": "تاجر تجريبي",
                "saved_usdt_address": None,
                "saved_payment_info": None,
                "kyc_status": "verified",
                "reward_points": 10,
                "reputation_score": "100.00",
                "completed_deals_count": 3,
                "is_banned": False,
                "created_at": "2026-11-20T12:00:00+00:00",
            }
        ],
        "offers": [],
        "deals": [],
        "negotiations": [],
        "disputes": [],
        "deal_ratings": [],
        "payment_proofs": [],
        "blockchain_transactions": [],
        "system_settings": [],
        "audit_logs": [],
        "kyc_requests": [],
    }


def test_backup_format_version_is_4_1():
    assert backup_service.BACKUP_FORMAT_VERSION == "4.1"


def test_encrypt_and_decrypt_backup_roundtrip_in_memory():
    key = Fernet.generate_key().decode("utf-8")
    payload = _sample_backup_payload("4.1")

    cipher_bytes = backup_service.encrypt_backup_payload(payload, raw_key=key)
    assert isinstance(cipher_bytes, bytes)
    # Must neither be plaintext JSON nor raw gzip
    assert not cipher_bytes.startswith(b"{")
    assert not cipher_bytes.startswith(b"\x1f\x8b")
    assert b"trader_alpha" not in cipher_bytes

    restored = restore_backup.decrypt_and_decompress_backup_bytes(cipher_bytes, raw_key=key)
    assert restored == payload


def test_missing_or_invalid_encryption_key_raises(monkeypatch):
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)

    with pytest.raises(backup_service.BackupEncryptionError):
        backup_service.encrypt_backup_payload(_sample_backup_payload())

    with pytest.raises(backup_service.BackupEncryptionError):
        backup_service.encrypt_backup_payload(_sample_backup_payload(), raw_key="not-a-valid-fernet-key")

    key1 = Fernet.generate_key().decode("utf-8")
    key2 = Fernet.generate_key().decode("utf-8")
    cipher_bytes = backup_service.encrypt_backup_payload(_sample_backup_payload(), raw_key=key1)

    with pytest.raises(ValueError):
        restore_backup.decrypt_and_decompress_backup_bytes(cipher_bytes)

    with pytest.raises(ValueError):
        restore_backup.decrypt_and_decompress_backup_bytes(cipher_bytes, raw_key=key2)


def test_write_encrypted_backup_retention_and_legacy_preservation(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", key)
    monkeypatch.setattr(backup_service, "BACKUP_DIR", str(tmp_path))

    # Create a legacy unencrypted .json.gz file that retention MUST NEVER delete
    legacy_file = tmp_path / "escrow_backup_20250101_000000.json.gz"
    legacy_file.write_bytes(gzip.compress(b"{}"))

    # Seed 31 older .json.gz.enc files with deterministic mtimes
    base_ts = 1_700_000_000
    for i in range(31):
        p = tmp_path / f"escrow_backup_20260101_{i:06d}.json.gz.enc"
        p.write_bytes(b"old-cipher")
        os.utime(p, (base_ts + i, base_ts + i))

    new_filepath = str(tmp_path / "escrow_backup_20261124_120000.json.gz.enc")
    payload = _sample_backup_payload("4.1")
    backup_service._write_encrypted_backup_and_cleanup(new_filepath, payload)

    assert os.path.isfile(new_filepath)
    assert legacy_file.is_file()

    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(new_filepath).st_mode)
        assert mode == 0o600

    enc_files = sorted(f for f in os.listdir(tmp_path) if f.endswith(".json.gz.enc"))
    assert len(enc_files) == 30
    assert "escrow_backup_20261124_120000.json.gz.enc" in enc_files
    # The two oldest files (index 0 and 1) must have been pruned
    assert "escrow_backup_20260101_000000.json.gz.enc" not in enc_files
    assert "escrow_backup_20260101_000001.json.gz.enc" not in enc_files

    loaded = restore_backup.load_and_validate_backup(new_filepath, encryption_key=key)
    assert loaded == payload


def test_write_encrypted_backup_fails_without_creating_file_when_key_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(backup_service, "BACKUP_DIR", str(tmp_path))

    filepath = tmp_path / "escrow_backup_20261124_130000.json.gz.enc"
    with pytest.raises(backup_service.BackupEncryptionError):
        backup_service._write_encrypted_backup_and_cleanup(str(filepath), _sample_backup_payload())

    assert not filepath.exists()


def test_load_and_validate_legacy_gzip_backup_defaults_missing_optional_tables(tmp_path):
    legacy_payload = {
        "metadata": {"created_at": "2025-10-01T00:00:00+00:00", "version": "3.9"},
        "users": [],
        "offers": [],
        "deals": [],
    }
    legacy_path = tmp_path / "escrow_backup_legacy.json.gz"
    with gzip.open(legacy_path, "wb") as f:
        f.write(json.dumps(legacy_payload).encode("utf-8"))

    loaded = restore_backup.load_and_validate_backup(str(legacy_path))
    assert loaded["kyc_requests"] == []
    assert loaded["blockchain_transactions"] == []
    assert loaded["audit_logs"] == []


def test_load_and_validate_rejects_unsupported_version_or_missing_core_table(tmp_path):
    bad_ver_path = tmp_path / "bad_ver.json.gz"
    with gzip.open(bad_ver_path, "wb") as f:
        f.write(json.dumps(_sample_backup_payload("1.0")).encode("utf-8"))

    with pytest.raises(ValueError, match="Unsupported backup format version"):
        restore_backup.load_and_validate_backup(str(bad_ver_path))

    missing_core_path = tmp_path / "missing_core.json.gz"
    bad_payload = {"metadata": {"version": "4.1"}, "users": [], "offers": []}
    with gzip.open(missing_core_path, "wb") as f:
        f.write(json.dumps(bad_payload).encode("utf-8"))

    with pytest.raises(ValueError, match="missing core table section 'deals'"):
        restore_backup.load_and_validate_backup(str(missing_core_path))


def test_restore_schema_tables_include_kyc_and_upgraded_blockchain_fields():
    ordered_names = [name for name, _ in restore_backup.ORDERED_TABLES]
    assert "kyc_requests" in ordered_names
    assert "kyc_requests" in restore_backup.SERIAL_ID_TABLES
    assert {"reviewed_at", "created_at"} <= restore_backup.DATETIME_FIELDS["kyc_requests"]
    assert {"amount", "amount_raw"} <= restore_backup.DECIMAL_FIELDS["blockchain_transactions"]
    assert {"confirmed_at", "created_at"} <= restore_backup.DATETIME_FIELDS["blockchain_transactions"]


def test_parse_row_backfills_legacy_blockchain_tx_and_deals():
    legacy_tx = {
        "id": 7,
        "deal_id": 10,
        "tx_hash": "0x" + "ab" * 32,
        "direction": "deposit",
        "amount": "150.25",
        "confirmed_at": "2026-11-20T10:00:00+00:00",
        "unknown_extra_col": "ignored",
    }
    parsed_tx = restore_backup._parse_row("blockchain_transactions", BlockchainTransaction, legacy_tx)
    assert parsed_tx["log_index"] == 0
    assert parsed_tx["status"] == "confirmed"
    assert parsed_tx["amount"] == Decimal("150.25")
    assert parsed_tx["confirmed_at"] == datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    assert parsed_tx["created_at"] == parsed_tx["confirmed_at"]
    assert "unknown_extra_col" not in parsed_tx

    legacy_deal = {
        "id": 10,
        "offer_id": 1,
        "seller_id": 100,
        "buyer_id": 200,
        "amount_usdt": "100.00",
        "amount_syp": "15000",
        "fee_usdt": None,
        "status": "COMPLETED",
    }
    parsed_deal = restore_backup._parse_row("deals", Deal, legacy_deal)
    assert parsed_deal["fee_usdt"] == Decimal("0.0")
    assert parsed_deal["seller_fee_usdt"] == Decimal("0.0")
    assert parsed_deal["buyer_fee_usdt"] == Decimal("0.0")
