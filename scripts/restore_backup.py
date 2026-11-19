#!/usr/bin/env python3
"""
Standalone CLI utility to restore an Alpha Bot database backup (.json.gz).
Never imported or invoked automatically by the Telegram bot runtime.
"""
import argparse
import asyncio
import gzip
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from typing import Any

# Ensure project root is on sys.path when invoked as `python scripts/restore_backup.py`
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.models import (
    AuditLog,
    BlockchainTransaction,
    Deal,
    DealRating,
    Dispute,
    Negotiation,
    Offer,
    PaymentProof,
    SystemSetting,
    User,
)
from db.session import engine, ensure_schema_updated

SUPPORTED_MAJOR_VERSIONS = {"3.8", "3.9"}

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
]

DECIMAL_FIELDS = {
    "users": {"reputation_score"},
    "offers": {"rate", "min_limit", "max_limit"},
    "deals": {"amount_usdt", "amount_syp", "fee_usdt", "seller_fee_usdt", "buyer_fee_usdt"},
    "negotiations": {"proposed_rate", "proposed_amount"},
    "payment_proofs": {"amount"},
    "blockchain_transactions": {"amount"},
}

DATETIME_FIELDS = {
    "users": {"created_at"},
    "offers": {"status_changed_at", "expires_at", "created_at"},
    "deals": {"expires_at", "created_at", "updated_at"},
    "negotiations": {"expires_at", "created_at", "updated_at"},
    "disputes": {"resolved_at"},
    "deal_ratings": {"created_at"},
    "payment_proofs": {"submitted_at"},
    "blockchain_transactions": {"confirmed_at"},
    "system_settings": {"updated_at"},
    "audit_logs": {"created_at"},
}


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
    return cleaned


def load_and_validate_backup(filepath: str) -> dict[str, Any]:
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"Backup file not found: {filepath}")

    with gzip.open(filepath, "rb") as f:
        payload = json.loads(f.read().decode("utf-8"))

    if not isinstance(payload, dict) or "metadata" not in payload:
        raise ValueError("Invalid backup structure: missing 'metadata' key.")

    meta = payload["metadata"]
    version = str(meta.get("version", ""))
    if version not in SUPPORTED_MAJOR_VERSIONS:
        raise ValueError(
            f"Unsupported backup format version '{version}'. Supported versions: {sorted(SUPPORTED_MAJOR_VERSIONS)}"
        )

    for tbl_name, _ in ORDERED_TABLES:
        if tbl_name not in payload:
            raise ValueError(f"Invalid backup structure: missing table section '{tbl_name}'.")
        if not isinstance(payload[tbl_name], list):
            raise ValueError(f"Invalid backup structure: section '{tbl_name}' must be a list.")

    return payload


async def run_restore(filepath: str, dry_run: bool = False, force: bool = False) -> int:
    try:
        payload = load_and_validate_backup(filepath)
    except Exception as exc:
        print(f"[ERROR] Validation failed: {exc}", file=sys.stderr)
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

    print(f"[INFO] Backup File      : {os.path.basename(filepath)}")
    print(f"[INFO] Format Version   : {version}")
    print(f"[INFO] Created At       : {created_at}")
    print("-" * 50)
    for tbl_name, _ in ORDERED_TABLES:
        print(f"[TABLE] {tbl_name:<25} : {len(payload.get(tbl_name, []))} rows")
    print(
        f"[SEQ]   deposit_index_seq        : target={target_deposit_seq} "
        f"(saved={saved_seq_val}, max_deal_index={max_deal_deposit_idx})"
    )
    print("-" * 50)

    if dry_run:
        print("[DRY-RUN] Validation complete. No database changes were made.")
        return 0

    await ensure_schema_updated()

    try:
        async with engine.begin() as conn:
            # 1. Check whether target tables are empty
            non_empty: list[tuple[str, int]] = []
            for tbl_name, _ in ORDERED_TABLES:
                res = await conn.execute(text(f"SELECT COUNT(*) FROM {tbl_name}"))
                cnt = int(res.scalar() or 0)
                if cnt > 0:
                    non_empty.append((tbl_name, cnt))

            if non_empty and not force:
                summary = ", ".join(f"{t}({c})" for t, c in non_empty)
                print(
                    f"[ABORT] Target database tables are not empty: {summary}. "
                    "Use --force to truncate and overwrite existing data.",
                    file=sys.stderr,
                )
                return 2

            if non_empty and force:
                # Truncate in reverse dependency order inside the same transaction
                rev_names = ", ".join(t for t, _ in reversed(ORDERED_TABLES))
                await conn.execute(text(f"TRUNCATE TABLE {rev_names} RESTART IDENTITY CASCADE"))
                print("[INFO] Existing data truncated (--force).")

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

        print("[SUCCESS] Database restore completed atomically.")
        return 0
    except Exception as exc:
        print(f"[ERROR] Restore transaction rolled back due to error: {type(exc).__name__}", file=sys.stderr)
        return 3
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore Alpha Bot PostgreSQL database from a .json.gz backup file."
    )
    parser.add_argument("backup_file", help="Path to the .json.gz backup file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate backup file and print expected row counts without touching the database",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Truncate and overwrite target tables if they are not empty",
    )
    args = parser.parse_args()
    exit_code = asyncio.run(run_restore(args.backup_file, dry_run=args.dry_run, force=args.force))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
