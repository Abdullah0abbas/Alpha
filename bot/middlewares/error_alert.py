import os
import time
import traceback
import logging
from typing import Dict, Any
from aiogram import Bot
from aiogram.types import ErrorEvent, Update
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramBadRequest,
    TelegramForbiddenError,
    RestartingTelegram
)

from bot.services.audit_service import audit_logger
from bot.utils.sanitize import sanitize_exception_message

logger = logging.getLogger(__name__)

# Throttling dictionary: error_signature -> last_alert_timestamp
_recent_errors: Dict[str, float] = {}
ALERT_THROTTLE_SECONDS = 60.0  # Alert at most once per minute for identical errors


def is_routine_error(exception: Exception) -> bool:
    """
    Returns True if the exception is a routine, transient network/API issue that should be ignored.
    Database errors from sqlalchemy, asyncpg, psycopg, or psycopg2 are NEVER routine.
    """
    # 0. Check if exception or any cause/context originates from database libraries
    curr: Any = exception
    visited = set()
    while curr and id(curr) not in visited:
        visited.add(id(curr))
        mod = getattr(type(curr), "__module__", "") or ""
        root_mod = mod.split(".")[0].lower()
        if root_mod in ("sqlalchemy", "asyncpg", "psycopg", "psycopg2"):
            return False
        curr = getattr(curr, "__cause__", None) or getattr(curr, "__context__", None)

    # 1. Direct type checks for network & transient Telegram errors
    if isinstance(exception, (TelegramNetworkError, TelegramRetryAfter, RestartingTelegram, TimeoutError)):
        return True

    # 2. TelegramForbiddenError (user blocked bot or deactivated account)
    if isinstance(exception, TelegramForbiddenError):
        return True

    # 3. TelegramBadRequest checks (except entity parsing errors, which indicate unescaped text bugs)
    if isinstance(exception, TelegramBadRequest):
        msg = str(exception).lower()
        ignorable_snippets = [
            "message is not modified",
            "message to edit not found",
            "message can't be edited",
            "message can't be deleted",
            "message to delete not found",
            "query is too old",
            "message not found",
            "bot was blocked by the user",
            "user is deactivated",
            "chat not found",
            "have no rights to send a message",
            "need administrator rights"
        ]
        if any(snippet in msg for snippet in ignorable_snippets):
            return True

    # 4. Standard library network errors
    err_str = str(exception).lower()
    transient_terms = [
        "clientconnecterror",
        "serverdisconnectederror",
        "connection reset by peer",
        "connection refused",
        "network is unreachable",
        "temporary failure in name resolution",
        "ssl: sslv3_alert_close_notify",
        "remotedisconnected",
        "cannot connect to host"
    ]
    if any(term in err_str for term in transient_terms):
        return True

    return False


def _extract_project_location(exception: Exception) -> tuple[str, str | int, str, str]:
    """
    Walks the traceback and returns (file_location, line_number, func_name, code_line)
    prioritizing the deepest frame inside our project code (/bot/, /db/, /config/, /watcher/).
    """
    tb_list = traceback.extract_tb(exception.__traceback__) if exception.__traceback__ else []
    relevant_frame = tb_list[-1] if tb_list else None
    for frame in reversed(tb_list):
        norm_path = frame.filename.replace("\\", "/")
        if "site-packages" not in norm_path and "dist-packages" not in norm_path:
            if any(seg in norm_path for seg in ("/bot/", "/db/", "/config/", "/watcher/", "app.py")):
                relevant_frame = frame
                break

    file_location = "غير محدد"
    line_number: str | int = "—"
    func_name = "—"
    code_line = ""
    if relevant_frame:
        file_location = os.path.basename(relevant_frame.filename)
        line_number = relevant_frame.lineno
        func_name = relevant_frame.name
        code_line = (relevant_frame.line or "").strip()

    return file_location, line_number, func_name, code_line


def _should_throttle_error(error_signature: str) -> bool:
    now = time.time()
    if len(_recent_errors) > 200:
        cutoff = now - 3600.0
        stale_keys = [k for k, ts in _recent_errors.items() if ts < cutoff]
        for k in stale_keys:
            _recent_errors.pop(k, None)

    last_time = _recent_errors.get(error_signature, 0.0)
    if now - last_time < ALERT_THROTTLE_SECONDS:
        return True
    _recent_errors[error_signature] = now
    return False


async def report_handled_exception(
    bot: Bot | None,
    exception: Exception,
    context_label: str = "عملية داخلية / خلفية",
    user_id: int | None = None,
) -> None:
    """
    Reports exceptions caught inside try/except blocks (e.g. background tasks in tasks.py,
    post-commit notifications, or channel dispatches) directly to the Admin Errors Channel
    (ADMIN_ERRORS_CHANNEL_ID) or ADMIN_IDS so developers never have to hunt through server logs.
    Routine transient Telegram errors (e.g., user blocked bot) are automatically ignored.
    """
    if is_routine_error(exception):
        return

    file_location, line_number, func_name, code_line = _extract_project_location(exception)
    exc_type = type(exception).__name__
    exc_message = sanitize_exception_message(exception, max_len=280)

    error_signature = f"HANDLED:{exc_type}:{file_location}:{line_number}:{exc_message[:60]}"
    if _should_throttle_error(error_signature):
        return

    audit_logger.error(
        f"HANDLED_SYSTEM_ERROR | ctx={context_label} | user={user_id} | at={file_location}:{line_number} in {func_name} | {exc_type}: {exc_message}",
        exc_info=exception
    )

    if not bot:
        return

    user_line = f"• المستخدم المرتبط: {user_id}\n" if user_id else ""
    code_snippet_line = f"• السطر البرمجي: {code_line[:120]}\n" if code_line else ""

    alert_text = (
        "🚨 إنذار خطأ برمجي ملتقط (Handled / Background Error)\n\n"
        f"• السياق / المكان: {context_label}\n"
        f"{user_line}\n"
        "⚠️ تفاصيل الاستثناء:\n"
        f"• نوع الخطأ: {exc_type}\n"
        f"• الموقع الدقيق: {file_location}:{line_number} في {func_name}()\n"
        f"{code_snippet_line}"
        f"• الرسالة: {exc_message}\n\n"
        "📁 تم تسجيل الـ Traceback الكامل في ملف السجلات escrow_system.log."
    )

    try:
        from bot.services.admin_notifier import dispatch_admin_notification
        await dispatch_admin_notification(
            bot=bot,
            category="errors",
            text=alert_text,
            parse_mode=None
        )
    except Exception as send_err:
        logger.error(f"Failed to dispatch handled error alert: {send_err}")


async def handle_critical_error(event: ErrorEvent, bot: Bot):
    """
    Global error handler registered on dp.errors:
    - Filters out routine transient network/telegram errors.
    - Captures critical database, logic, and operational exceptions.
    - Logs detailed traceback to the rotating system log with sanitized error message.
    - Sends an immediate, plain-text Telegram alert to Admin Errors Channel / Admins with deduplication.
    """
    exception = event.exception
    update: Update = event.update

    # Step 1: Filter routine errors
    if is_routine_error(exception):
        logger.debug(f"Ignored routine error: {type(exception).__name__}: {exception}")
        return

    # Step 2: Extract User & Context Details (never leak raw user message contents)
    user_id = None
    username = None
    action_type = "غير محدد"
    action_payload = ""

    if update:
        if update.message:
            m = update.message
            user_id = m.from_user.id if m.from_user else None
            username = m.from_user.username if m.from_user else None
            action_type = "رسالة"
            if m.text:
                action_payload = f"رسالة نصية ({len(m.text)} حرفاً)"
            elif m.caption:
                action_payload = f"وسائط ({len(m.caption)} حرفاً)"
            else:
                action_payload = "وسائط"
            try:
                await m.answer("⚠️ عذراً، حدث خطأ تقني مؤقت وتم إشعار الإدارة تلقائياً للمتابعة.")
            except Exception:
                pass
        elif update.callback_query:
            cq = update.callback_query
            user_id = cq.from_user.id if cq.from_user else None
            username = cq.from_user.username if cq.from_user else None
            action_type = "زر تفاعلي (Callback)"
            action_payload = cq.data or ""
            try:
                await cq.answer("⚠️ عذراً، حدث خطأ تقني مؤقت وتم إشعار الإدارة تلقائياً.", show_alert=True)
            except Exception:
                pass

    # Step 3: Extract traceback & code location
    file_location, line_number, func_name, code_line = _extract_project_location(exception)
    exc_type = type(exception).__name__
    exc_message = sanitize_exception_message(exception, max_len=280)

    # Step 4: De-duplication / Throttle check
    error_signature = f"UNHANDLED:{exc_type}:{file_location}:{line_number}:{exc_message[:60]}"
    if _should_throttle_error(error_signature):
        logger.warning(f"Throttled duplicate error alert: {error_signature}")
        return

    # Step 5: Log to persistent rotating system log file
    audit_logger.error(
        f"CRITICAL_SYSTEM_ERROR | user={user_id} | at={file_location}:{line_number} in {func_name} | {exc_type}: {exc_message}",
        exc_info=exception
    )

    # Step 6: Format high-priority Plain-Text Telegram alert for Admins (parse_mode=None, no markdown)
    user_str = f"@{username} ({user_id})" if username else (f"{user_id}" if user_id else "غير متاح")
    code_snippet_line = f"• السطر البرمجي: {code_line[:120]}\n" if code_line else ""

    alert_text = (
        "🚨 إنذار خطأ تقني حساس في النظام\n\n"
        f"• المستخدم المتأثر: {user_str}\n"
        f"• نوع الإجراء: {action_type}\n"
        f"• البيانات/الأمر: {action_payload[:100]}\n\n"
        "⚠️ تفاصيل الاستثناء:\n"
        f"• نوع الخطأ: {exc_type}\n"
        f"• الموقع الدقيق: {file_location}:{line_number} في {func_name}()\n"
        f"{code_snippet_line}"
        f"• الرسالة: {exc_message}\n\n"
        "📁 تم تسجيل التفاصيل الكاملة و trace في ملف السجلات escrow_system.log."
    )

    # Step 7: Dispatch alert to Admin Errors Channel (ADMIN_ERRORS_CHANNEL_ID) or fallback to ADMIN_IDS
    from bot.services.admin_notifier import dispatch_admin_notification
    await dispatch_admin_notification(
        bot=bot,
        category="errors",
        text=alert_text,
        parse_mode=None
    )
