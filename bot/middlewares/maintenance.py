import time
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery, Update
from sqlalchemy import select
from db.models import SystemSetting
from config.settings import settings

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
                "⚠️ **Alpha Bot | صيانة مجدولة**\n\n"
                "النظام متوقف مؤقتاً لإجراء تحديثات وتحسينات دورية.\n"
                "يرجى المحاولة بعد قليل. 🛠️"
            )
            if isinstance(actual_event, CallbackQuery):
                try:
                    await actual_event.answer("⚠️ Alpha Bot في وضع الصيانة المجدولة حالياً.", show_alert=True)
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
