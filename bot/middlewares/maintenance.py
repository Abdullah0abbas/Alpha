import logging
import time
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery, Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from db.models import SystemSetting
from db.session import AsyncSessionLocal
from config.settings import settings

logger = logging.getLogger(__name__)

# In-memory cache for maintenance mode with TTL (3 seconds) to avoid querying DB on every single update
_maintenance_cache: Dict[str, Any] = {
    "is_enabled": False,
    "last_checked": 0.0
}

def get_cached_maintenance() -> bool:
    return _maintenance_cache["is_enabled"]

def set_cached_maintenance(val: bool):
    _maintenance_cache["is_enabled"] = val
    _maintenance_cache["last_checked"] = time.time()


def _parse_maintenance_setting(setting: SystemSetting | None) -> bool:
    if setting and isinstance(setting.value, dict):
        return bool(setting.value.get("enabled", False))
    if setting and isinstance(setting.value, bool):
        return setting.value
    return False


async def is_maintenance_active(
    session: AsyncSession | None = None,
    *,
    bypass_cache: bool = False,
) -> bool:
    """
    Central async helper for background workers (and financial tasks in A1/B1/B2)
    to check whether system maintenance mode is currently active.
    Uses the 3-second TTL cache unless bypass_cache=True, and queries SystemSetting('maintenance_mode')
    via the provided session or a short-lived AsyncSessionLocal session.
    """
    now = time.time()
    if not bypass_cache and (now - _maintenance_cache["last_checked"] <= 3.0):
        return bool(_maintenance_cache["is_enabled"])

    try:
        if session is not None:
            result = await session.execute(
                select(SystemSetting).where(SystemSetting.key == "maintenance_mode")
            )
            setting = result.scalar_one_or_none()
        else:
            async with AsyncSessionLocal() as db_sess:
                result = await db_sess.execute(
                    select(SystemSetting).where(SystemSetting.key == "maintenance_mode")
                )
                setting = result.scalar_one_or_none()

        is_enabled = _parse_maintenance_setting(setting)
        set_cached_maintenance(is_enabled)
        return is_enabled
    except Exception as exc:
        logger.warning(f"is_maintenance_active DB check failed, falling back to cache: {exc}")
        return bool(_maintenance_cache["is_enabled"])


class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Extract underlying event if registered on dp.update
        actual_event = event
        if isinstance(event, Update):
            actual_event = event.message or event.callback_query or event.inline_query or event

        # Determine user ID
        user_id = None
        if hasattr(actual_event, "from_user") and actual_event.from_user:
            user_id = actual_event.from_user.id

        # Admins always bypass maintenance mode completely
        if user_id and user_id in settings.ADMIN_IDS:
            return await handler(event, data)

        # Check maintenance status
        now = time.time()
        is_maintenance = _maintenance_cache["is_enabled"]

        if now - _maintenance_cache["last_checked"] > 3.0:
            db_session = data.get("db_session")
            if db_session:
                try:
                    result = await db_session.execute(
                        select(SystemSetting).where(SystemSetting.key == "maintenance_mode")
                    )
                    setting = result.scalar_one_or_none()
                    if setting and isinstance(setting.value, dict):
                        is_maintenance = bool(setting.value.get("enabled", False))
                    elif setting and isinstance(setting.value, bool):
                        is_maintenance = setting.value
                    else:
                        is_maintenance = False
                    set_cached_maintenance(is_maintenance)
                except Exception:
                    # In case of DB hiccup, fallback to existing cache
                    pass

        if is_maintenance:
            maintenance_text = (
                "🛠️ **صيانة مجدولة — Alpha Bot**\n\n"
                "النظام متوقف مؤقتاً لإجراء تحديثات وتحسينات دورية.\n"
                "يرجى المحاولة بعد قليل."
            )
            if isinstance(actual_event, CallbackQuery):
                try:
                    await actual_event.answer("⚠️ البوت في وضع الصيانة المجدولة حالياً.", show_alert=True)
                except Exception:
                    pass
                return
            elif isinstance(actual_event, Message):
                try:
                    await actual_event.answer(maintenance_text, parse_mode="Markdown")
                except Exception:
                    pass
                return

        return await handler(event, data)
