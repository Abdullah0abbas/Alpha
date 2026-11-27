#!/usr/bin/env python3
"""
Standalone CLI utility to restore an Alpha Bot database backup (.json.gz.enc or legacy .json.gz).
Never imported or invoked automatically by the Telegram bot runtime.
"""
import argparse
import asyncio
import gzip
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

# Ensure project root is on sys.path when invoked as `python scripts/restore_backup.py`
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bot.utils.sanitize import sanitize_exception_message
from config.settings import settings
from db.models import (
    AuditLog,
    BlockchainTransaction,
    Deal,
    DealRating,
    Dispute,
    KycRequest,
    Negotiation,
    Offer,
    PaymentProof,
    SystemSetting,
    User,
)
from db.session import engine, ensure_schema_updated

# Core tables required in every backup version; newer tables default to [] if absent in older 2.x/3.x/4.x backups
CORE_REQUIRED_TABLES = {"users", "offers", "deals"}

# Strict FK-safe restoration order
ORDERED_TABLES = [
    ("users", User),
    ("offers", Offer),
    ("deals", Deal),
    ("negotiations", Negotiation),
    ("payment_proofs", PaymentProof),
    ("disputes", Dispute),
    ("deal_ratings", DealRating),
    ("blockchain_transactions", BlockchainTransaction),
    ("system_settings", SystemSetting),
    ("audit_logs", AuditLog),
    ("kyc_requests", KycRequest),
]

# Tables that use an integer SERIAL/sequence primary key `id`
SERIAL_ID_TABLES = [
    "offers",
    "deals",
    "negotiations",
    "payment_proofs",
    "disputes",
    "deal_ratings",
    "blockchain_transactions",
    "audit_logs",
    "kyc_requests",
]

DECIMAL_FIELDS = {
    "users": {"reputation_score"},
    "offers": {"rate", "min_limit", "max_limit", "duration_hours"},
    "deals": {"amount_usdt", "amount_syp", "fee_usdt", "seller_fee_usdt", "buyer_fee_usdt"},
    "negotiations": {"proposed_rate", "proposed_amount"},
    "payment_proofs": {"amount"},
    "blockchain_transactions": {"amount", "amount_raw"},
}

DATETIME_FIELDS = {
    "users": {"created_at"},
    "offers": {"status_changed_at", "expires_at", "created_at"},
    "deals": {"expires_at", "created_at", "updated_at"},
    "negotiations": {"expires_at", "created_at", "updated_at"},
    "disputes": {"resolved_at"},
    "deal_ratings": {"created_at"},
    "payment_proofs": {"submitted_at"},
    "blockchain_transactions": {"confirmed_at", "created_at"},
    "system_settings": {"updated_at"},
    "audit_logs": {"created_at"},
    "kyc_requests": {"reviewed_at", "created_at"},
}


def _get_target_db_display() -> tuple[str, str]:
    """Returns (host_with_port, database_name) safely without exposing credentials."""
    url = engine.url
    host = url.host or "localhost"
    port = f":{url.port}" if url.port else ""
    db_name = url.database or "unknown_db"
    return f"{host}{port}", db_name


def _parse_row(table_name: str, model_cls: Any, raw_row: dict[str, Any]) -> dict[str, Any]:
    valid_cols = {c.name for c in model_cls.__table__.columns}
    dec_cols = DECIMAL_FIELDS.get(table_name, set())
    dt_cols = DATETIME_FIELDS.get(table_name, set())

    cleaned: dict[str, Any] = {}
    for k, v in raw_row.items():
        if k not in valid_cols:
            continue
        if v is None:
            cleaned[k] = None
        elif k in dec_cols and isinstance(v, (str, int, float)):
            cleaned[k] = Decimal(str(v))
        elif k in dt_cols and isinstance(v, str):
            cleaned[k] = datetime.fromisoformat(v)
        else:
            cleaned[k] = v

    if table_name == "users":
        if cleaned.get("yellow_cards") is None:
            cleaned["yellow_cards"] = 0

    # Backfill NOT NULL fee columns if restoring from an older 2.x/3.x backup where they were absent or null
    if table_name == "deals":
        if cleaned.get("fee_usdt") is None:
            cleaned["fee_usdt"] = Decimal("0.0")
        if cleaned.get("seller_fee_usdt") is None:
            cleaned["seller_fee_usdt"] = Decimal("0.0")
        if cleaned.get("buyer_fee_usdt") is None:
            cleaned["buyer_fee_usdt"] = Decimal("0.0")

    # Backfill NOT NULL columns if restoring blockchain_transactions from an older 2.x/3.x/4.0 backup
    if table_name == "blockchain_transactions":
        if cleaned.get("log_index") is None:
            cleaned["log_index"] = 0
        if not cleaned.get("status"):
            cleaned["status"] = "confirmed"
        if cleaned.get("created_at") is None:
            cleaned["created_at"] = cleaned.get("confirmed_at") or datetime.now(timezone.utc)

    return cleaned


def decrypt_and_decompress_backup_bytes(
    cipher_bytes: bytes, raw_key: str | None = None
) -> dict[str, Any]:
    """
    Decrypts Fernet ciphertext and decompresses gzip JSON payload strictly in memory.
    Raises ValueError if BACKUP_ENCRYPTION_KEY is missing/invalid or ciphertext is corrupted.
    """
    key_str = (
        raw_key
        if raw_key is not None
        else (os.getenv("BACKUP_ENCRYPTION_KEY") or settings.BACKUP_ENCRYPTION_KEY or "")
    ).strip()
    if not key_str:
        raise ValueError(
            "Encrypted backup (.enc) requires BACKUP_ENCRYPTION_KEY in environment or .env."
        )
    try:
        fernet = Fernet(key_str.encode("utf-8"))
    except Exception as exc:
        raise ValueError(
            "Invalid BACKUP_ENCRYPTION_KEY format (expected 32-byte URL-safe Base64 Fernet key)."
        ) from exc

    try:
        compressed_bytes = fernet.decrypt(cipher_bytes)
    except InvalidToken as exc:
        raise ValueError(
            "Failed to decrypt backup file: invalid BACKUP_ENCRYPTION_KEY or corrupted ciphertext."
        ) from exc

    raw_json_bytes = gzip.decompress(compressed_bytes)
    return json.loads(raw_json_bytes.decode("utf-8"))


def load_and_validate_backup(filepath: str, encryption_key: str | None = None) -> dict[str, Any]:
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"Backup file not found: {filepath}")

    if filepath.endswith(".enc"):
        with open(filepath, "rb") as f:
            cipher_bytes = f.read()
        payload = decrypt_and_decompress_backup_bytes(cipher_bytes, raw_key=encryption_key)
    else:
        with gzip.open(filepath, "rb") as f:
            payload = json.loads(f.read().decode("utf-8"))

    if not isinstance(payload, dict) or "metadata" not in payload:
        raise ValueError("Invalid backup structure: missing 'metadata' key.")

    meta = payload["metadata"]
    version = str(meta.get("version", "")).strip()
    if not version or not (
        version.startswith("2.") or version.startswith("3.") or version.startswith("4.")
    ):
        raise ValueError(
            f"Unsupported backup format version '{version}'. Expected 2.x, 3.x, or 4.x backup format."
        )

    for tbl_name, _ in ORDERED_TABLES:
        if tbl_name not in payload:
            if tbl_name in CORE_REQUIRED_TABLES:
                raise ValueError(f"Invalid backup structure: missing core table section '{tbl_name}'.")
            payload[tbl_name] = []
        elif not isinstance(payload[tbl_name], list):
            raise ValueError(f"Invalid backup structure: section '{tbl_name}' must be a list.")

    return payload


async def run_restore(filepath: str, dry_run: bool = False, force: bool = False) -> int:
    try:
        payload = load_and_validate_backup(filepath)
    except Exception as exc:
        safe_msg = sanitize_exception_message(exc, max_len=300)
        print(f"[ERROR] Validation failed ({type(exc).__name__}): {safe_msg}", file=sys.stderr)
        return 1

    meta = payload["metadata"]
    version = meta.get("version", "unknown")
    created_at = meta.get("created_at", "unknown")
    seq_meta = meta.get("deposit_index_seq") or {}
    saved_seq_val = int(seq_meta.get("last_value") or 0)
    saved_seq_called = bool(seq_meta.get("is_called", False))

    deals_rows = payload.get("deals", [])
    max_deal_deposit_idx = max(
        (int(r["deposit_index"]) for r in deals_rows if r.get("deposit_index") is not None),
        default=0,
    )
    target_deposit_seq = max(saved_seq_val, max_deal_deposit_idx)
    db_host, db_name = _get_target_db_display()

    print(f"[INFO] Target Database  : {db_name} @ {db_host}")
    print(f"[INFO] Backup File      : {os.path.basename(filepath)}")
    print(f"[INFO] Format Version   : {version}")
    print(f"[INFO] Created At       : {created_at}")
    print("-" * 55)
    for tbl_name, _ in ORDERED_TABLES:
        print(f"[TABLE] {tbl_name:<25} : {len(payload.get(tbl_name, []))} rows")
    print(
        f"[SEQ]   deposit_index_seq        : target={target_deposit_seq} "
        f"(saved={saved_seq_val}, max_deal_index={max_deal_deposit_idx})"
    )
    print("-" * 55)

    if dry_run:
        print("[DRY-RUN] Validation complete. No database changes were made.")
        return 0

    await ensure_schema_updated()

    try:
        # 1. Check whether target tables are empty in a short read block (closed before interactive input)
        non_empty: list[tuple[str, int]] = []
        async with engine.connect() as read_conn:
            for tbl_name, _ in ORDERED_TABLES:
                res = await read_conn.execute(text(f"SELECT COUNT(*) FROM {tbl_name}"))
                cnt = int(res.scalar() or 0)
                if cnt > 0:
                    non_empty.append((tbl_name, cnt))

        if non_empty and not force:
            summary = ", ".join(f"{t}({c})" for t, c in non_empty)
            print(
                f"[ABORT] Target database '{db_name}' ({db_host}) is not empty: {summary}. "
                "Use --force to truncate and overwrite existing data.",
                file=sys.stderr,
            )
            return 2

        if non_empty and force:
            summary = ", ".join(f"{t}({c})" for t, c in non_empty)
            print(
                f"\n⚠️  [WARNING] You are about to PERMANENTLY WIPE all existing data in:\n"
                f"    • Host     : {db_host}\n"
                f"    • Database : {db_name}\n"
                f"    • Non-empty: {summary}\n"
            )
            try:
                confirm = input("Type 'YES' (in uppercase) to confirm destructive overwrite: ").strip()
            except EOFError:
                confirm = ""
            if confirm != "YES":
                print("[ABORT] Confirmation not received. Restore cancelled safely.", file=sys.stderr)
                return 2

        async with engine.begin() as conn:
            if non_empty and force:
                # Truncate in reverse dependency order inside the atomic write transaction
                rev_names = ", ".join(t for t, _ in reversed(ORDERED_TABLES))
                await conn.execute(text(f"TRUNCATE TABLE {rev_names} RESTART IDENTITY CASCADE"))
                print(f"[INFO] Existing data in '{db_name}' truncated (--force confirmed).")

            # 2. Insert rows in FK order preserving original primary keys
            for tbl_name, model_cls in ORDERED_TABLES:
                raw_rows = payload.get(tbl_name, [])
                if not raw_rows:
                    continue
                parsed_rows = [_parse_row(tbl_name, model_cls, r) for r in raw_rows]
                # Batch insert in chunks of 500 to avoid PostgreSQL bind parameter limits
                for idx in range(0, len(parsed_rows), 500):
                    chunk = parsed_rows[idx: idx + 500]
                    await conn.execute(pg_insert(model_cls.__table__), chunk)
                print(f"[RESTORED] {tbl_name:<25} : {len(parsed_rows)} rows inserted.")

            # 3. Reset table ID sequences according to MAX(id)
            for tbl_name in SERIAL_ID_TABLES:
                await conn.execute(
                    text(
                        f"""
                        SELECT setval(
                            pg_get_serial_sequence('{tbl_name}', 'id'),
                            COALESCE((SELECT MAX(id) FROM {tbl_name}), 1),
                            (SELECT COUNT(*) > 0 FROM {tbl_name})
                        )
                        """
                    )
                )

            # 4. Reset deposit_index_seq to max(saved_seq_val, max(deals.deposit_index))
            if target_deposit_seq > 0:
                is_called = True if max_deal_deposit_idx > 0 else saved_seq_called
                await conn.execute(
                    text("SELECT setval('deposit_index_seq', :val, :called)"),
                    {"val": target_deposit_seq, "called": is_called},
                )
            else:
                await conn.execute(text("SELECT setval('deposit_index_seq', 1, false)"))

        print(f"[SUCCESS] Database restore to '{db_name}' ({db_host}) completed atomically.")
        return 0
    except Exception as exc:
        safe_err = sanitize_exception_message(exc, max_len=300)
        print(
            f"[ERROR] Restore transaction rolled back ({type(exc).__name__}): {safe_err}",
            file=sys.stderr,
        )
        return 3
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore Alpha Bot PostgreSQL database from a .json.gz.enc (or legacy .json.gz) backup file."
    )
    parser.add_argument("backup_file", help="Path to the .json.gz.enc or .json.gz backup file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate backup file and print expected row counts without touching the database",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Truncate and overwrite target tables if they are not empty (requires interactive YES confirmation)",
    )
    args = parser.parse_args()
    exit_code = asyncio.run(run_restore(args.backup_file, dry_run=args.dry_run, force=args.force))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
