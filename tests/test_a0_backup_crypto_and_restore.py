"""
Offline unit tests for Phase A0-1, A0-2, and A0-5:
- Backup in-memory compression & Fernet encryption / decryption
- Missing or invalid BACKUP_ENCRYPTION_KEY rejection
- Backup format version validation (2.x, 3.x, 4.0, 4.1) for both .json.gz.enc and legacy .json.gz
- Complete ForeignKey coverage in ORDERED_TABLES (including kyc_requests)
- Legacy row default backfilling for blockchain_transactions and datetime parsing for kyc_requests
Requires zero network access and zero database connections.
"""
from datetime import datetime
import gzip
import json
from cryptography.fernet import Fernet
import pytest

from bot.services.backup_service import (
    BACKUP_FORMAT_VERSION,
    BackupEncryptionError,
    encrypt_backup_payload,
)
from db.models import Base, BlockchainTransaction, KycRequest
from scripts.restore_backup import (
    ORDERED_TABLES,
    SERIAL_ID_TABLES,
    _parse_row,
    decrypt_and_decompress_backup_bytes,
    load_and_validate_backup,
)


def _build_minimal_payload(version: str = "4.1") -> dict:
    return {
        "metadata": {
            "version": version,
            "created_at": "2026-11-24T00:00:00+00:00",
            "deposit_index_seq": {"last_value": 7, "is_called": True},
        },
        "users": [{"telegram_id": 111, "username": "alice"}],
        "offers": [],
        "deals": [],
        "kyc_requests": [
            {
                "id": 1,
                "user_id": 111,
                "full_name": "Test User",
                "national_id": "01010101010",
                "phone_number": "0999999999",
                "id_front_file_id": "f1",
                "id_back_file_id": "f2",
                "selfie_file_id": "f3",
                "status": "approved",
                "reviewed_by": 999,
                "reviewed_at": "2026-11-24T01:00:00+00:00",
                "created_at": "2026-11-24T00:30:00+00:00",
            }
        ],
    }


def test_backup_format_version_is_4_1():
    assert BACKUP_FORMAT_VERSION == "4.1"


def test_all_sqlalchemy_models_and_user_fks_in_ordered_tables():
    ordered_names = [name for name, _ in ORDERED_TABLES]
    assert "kyc_requests" in ordered_names
    assert "kyc_requests" in SERIAL_ID_TABLES

    # Every table defined in Base.metadata must be present in ORDERED_TABLES
    model_tables = set(Base.metadata.tables.keys())
    assert model_tables == set(ordered_names)

    # Every table with a ForeignKey to users must appear after 'users' in ORDERED_TABLES
    users_idx = ordered_names.index("users")
    for table_name, table_obj in Base.metadata.tables.items():
        for fk in table_obj.foreign_keys:
            if fk.column.table.name == "users":
                assert ordered_names.index(table_name) > users_idx


def test_in_memory_encrypt_and_decrypt_roundtrip():
    key = Fernet.generate_key().decode("utf-8")
    payload = _build_minimal_payload("4.1")

    cipher_bytes = encrypt_backup_payload(payload, raw_key=key)
    # Verify ciphertext is not plain JSON or plain gzip magic header (0x1f 0x8b)
    assert not cipher_bytes.startswith(b"\x1f\x8b")
    assert b"01010101010" not in cipher_bytes

    restored = decrypt_and_decompress_backup_bytes(cipher_bytes, raw_key=key)
    assert restored == payload


def test_encrypt_rejects_missing_or_invalid_key(monkeypatch):
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    payload = _build_minimal_payload("4.1")

    with pytest.raises(BackupEncryptionError):
        encrypt_backup_payload(payload, raw_key="")

    with pytest.raises(BackupEncryptionError):
        encrypt_backup_payload(payload, raw_key="not-a-valid-fernet-key")


def test_load_and_validate_encrypted_and_legacy_files(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    wrong_key = Fernet.generate_key().decode("utf-8")

    # 1. Encrypted .json.gz.enc across versions 2.0, 3.0, 4.0, 4.1
    for ver in ("2.0", "3.1", "4.0", "4.1"):
        enc_file = tmp_path / f"escrow_backup_{ver}.json.gz.enc"
        enc_file.write_bytes(encrypt_backup_payload(_build_minimal_payload(ver), raw_key=key))

        loaded = load_and_validate_backup(str(enc_file), encryption_key=key)
        assert loaded["metadata"]["version"] == ver
        assert len(loaded["kyc_requests"]) == 1

    # 2. Missing key or wrong key on .enc must raise ValueError
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    enc_41 = tmp_path / "escrow_backup_4.1.json.gz.enc"
    with pytest.raises(ValueError, match="requires BACKUP_ENCRYPTION_KEY"):
        load_and_validate_backup(str(enc_41), encryption_key="")

    with pytest.raises(ValueError, match="Failed to decrypt"):
        load_and_validate_backup(str(enc_41), encryption_key=wrong_key)

    # 3. Legacy unencrypted .json.gz still supported
    legacy_file = tmp_path / "escrow_backup_legacy.json.gz"
    with gzip.open(legacy_file, "wb") as f:
        f.write(json.dumps(_build_minimal_payload("4.0")).encode("utf-8"))

    loaded_legacy = load_and_validate_backup(str(legacy_file))
    assert loaded_legacy["metadata"]["version"] == "4.0"

    # 4. Unsupported version (e.g. 5.0) must be rejected
    bad_ver_file = tmp_path / "escrow_backup_5.0.json.gz.enc"
    bad_ver_file.write_bytes(encrypt_backup_payload(_build_minimal_payload("5.0"), raw_key=key))
    with pytest.raises(ValueError, match="Unsupported backup format version"):
        load_and_validate_backup(str(bad_ver_file), encryption_key=key)


def test_parse_row_legacy_blockchain_tx_and_kyc_datetimes():
    legacy_tx = {
        "id": 5,
        "deal_id": 22,
        "tx_hash": "0x1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
        "direction": "deposit",
        "amount": "125.50",
        "confirmed_at": "2026-11-20T10:15:00+00:00",
    }
    parsed_tx = _parse_row("blockchain_transactions", BlockchainTransaction, legacy_tx)
    assert parsed_tx["log_index"] == 0
    assert parsed_tx["status"] == "confirmed"
    assert isinstance(parsed_tx["created_at"], datetime)
    assert parsed_tx["created_at"] == parsed_tx["confirmed_at"]

    raw_kyc = _build_minimal_payload("4.1")["kyc_requests"][0]
    parsed_kyc = _parse_row("kyc_requests", KycRequest, raw_kyc)
    assert isinstance(parsed_kyc["reviewed_at"], datetime)
    assert isinstance(parsed_kyc["created_at"], datetime)
