import time
import traceback
import logging
from typing import Dict, Any
from aiogram import Bot
from aiogram.types import ErrorEvent, Message, CallbackQuery, Update
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramAPIError,
    RestartingTelegram
)

from bot.services.audit_service import audit_logger
from config.settings import settings

logger = logging.getLogger(__name__)

# Throttling dictionary: error_signature -> last_alert_timestamp
_recent_errors: Dict[str, float] = {}
ALERT_THROTTLE_SECONDS = 60.0  # Alert at most once per minute for identical errors

def is_routine_error(exception: Exception) -> bool:
    """
    Returns True if the exception is a routine, transient network/API issue that should be ignored:
    - Network connectivity drops, timeouts, socket disconnects.
    - Telegram flood rate limits (RetryAfter).
    - User-side benign errors: 'message is not modified', 'query is too old', 'bot was blocked by user'.
    """
    # 1. Direct type checks for network & transient Telegram errors
    if isinstance(exception, (TelegramNetworkError, TelegramRetryAfter, RestartingTelegram, TimeoutError)):
        return True

    # 2. TelegramForbiddenError (user blocked bot or deactivated account)
    if isinstance(exception, TelegramForbiddenError):
        return True

    # 3. TelegramBadRequest checks
    if isinstance(exception, TelegramBadRequest):
        msg = str(exception).lower()
        ignorable_snippets = [
            "message is not modified",
            "message to edit not found",
            "message can't be edited",
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


async def handle_critical_error(event: ErrorEvent, bot: Bot):
    """
    Global error handler registered on dp.errors:
    - Filters out routine transient network/telegram errors.
    - Captures critical database, logic, and operational exceptions.
    - Logs detailed traceback to the rotating system log.
    - Sends an immediate, structured Telegram alert to all Admins with deduplication.
    """
    exception = event.exception
    update: Update = event.update

    # Step 1: Filter routine errors
    if is_routine_error(exception):
        logger.debug(f"Ignored routine error: {type(exception).__name__}: {exception}")
        return

    # Step 2: Extract User & Context Details
    user_id = None
    username = None
    chat_id = None
    action_type = "غير محدد"
    action_payload = ""

    if update:
        if update.message:
            m = update.message
            user_id = m.from_user.id if m.from_user else None
            username = m.from_user.username if m.from_user else None
            chat_id = m.chat.id
            action_type = "رسالة نصية"
            action_payload = m.text or m.caption or "[وسائط]"
        elif update.callback_query:
            cq = update.callback_query
            user_id = cq.from_user.id if cq.from_user else None
            username = cq.from_user.username if cq.from_user else None
            chat_id = cq.message.chat.id if cq.message else None
            action_type = "زر تفاعلي (Callback)"
            action_payload = cq.data or ""

    # Step 3: Extract traceback & code location
    tb_list = traceback.extract_tb(exception.__traceback__)
    relevant_frame = tb_list[-1] if tb_list else None
    
    file_location = "غير محدد"
    line_number = "—"
    func_name = "—"
    if relevant_frame:
        file_location = relevant_frame.filename.split("/")[-1]
        line_number = relevant_frame.lineno
        func_name = relevant_frame.name

    exc_type = type(exception).__name__
    exc_message = str(exception)
    if len(exc_message) > 250:
        exc_message = exc_message[:247] + "..."

    # Step 4: De-duplication / Throttle check
    error_signature = f"{exc_type}:{file_location}:{line_number}:{exc_message[:60]}"
    now = time.time()
    last_time = _recent_errors.get(error_signature, 0.0)
    if now - last_time < ALERT_THROTTLE_SECONDS:
        logger.warning(f"Throttled duplicate error alert: {error_signature}")
        return
    _recent_errors[error_signature] = now

    # Step 5: Log to persistent rotating system log file
    audit_logger.error(
        f"CRITICAL_SYSTEM_ERROR | user={user_id} | at={file_location}:{line_number} in {func_name} | {exc_type}: {exc_message}",
        exc_info=exception
    )

    # Step 6: Format high-priority Telegram alert for Admins
    user_str = f"@{username} (`{user_id}`)" if username else (f"`{user_id}`" if user_id else "غير متاح")
    
    alert_text = (
        "🚨 **إنذار خطأ تقني حساس في النظام**\n\n\n"
        f"• **المستخدم المتأثر:**  {user_str}\n"
        f"• **نوع الإجراء:**  {action_type}\n"
        f"• **البيانات/الأمر:**  `{action_payload[:100]}`\n\n\n"
        f"⚠️ **تفاصيل الاستثناء:**\n"
        f"• نوع الخطأ:  `{exc_type}`\n"
        f"• الموقع:  `{file_location}:{line_number}` في `{func_name}()`\n"
        f"• الرسالة:  `{exc_message}`\n\n\n"
        "📁 تم تسجيل التفاصيل الكاملة و trace في ملف السجلات `escrow_system.log`."
    )

    # Step 7: Dispatch alert to all Admins
    for admin_id in settings.ADMIN_IDS:
        if not admin_id:
            continue
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=alert_text,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to deliver critical error alert to admin {admin_id}: {e}")
