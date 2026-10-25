import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession
from db.models import AuditLog

# Configure Rotating File Logger for Persistent System Logs
LOGS_DIR = os.path.join(os.getcwd(), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOGS_DIR, "escrow_system.log")

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=10 * 1024 * 1024, # 10 Megabytes per file
    backupCount=10,             # Keep up to 10 rotated log files
    encoding="utf-8"
)
file_formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
file_handler.setFormatter(file_formatter)
file_handler.setLevel(logging.INFO)

# Root audit logger
audit_logger = logging.getLogger("audit")
audit_logger.setLevel(logging.INFO)
if not audit_logger.handlers:
    audit_logger.addHandler(file_handler)

async def record_audit(
    session: AsyncSession,
    action: str,
    target_type: str,
    target_id: str | int,
    actor_id: int | None = None,
    actor_type: str = "ADMIN",
    details: dict[str, Any] | None = None,
    ip_or_source: str = "TELEGRAM_BOT"
) -> AuditLog:
    """
    Creates an immutable audit record in the database AND writes to the rotating system log.
    """
    target_id_str = str(target_id)
    
    # 1. Log to persistent file
    audit_logger.info(
        f"AUDIT | actor={actor_type}:{actor_id} | action={action} | target={target_type}:{target_id_str} | details={details}"
    )

    # 2. Persist to Database table
    log_entry = AuditLog(
        actor_id=actor_id,
        actor_type=actor_type,
        action=action,
        target_type=target_type,
        target_id=target_id_str,
        details=details or {},
        ip_or_source=ip_or_source
    )
    session.add(log_entry)
    await session.commit()
    return log_entry
