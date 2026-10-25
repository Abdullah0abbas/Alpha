import time
from typing import Any, Awaitable, Callable, Dict, List, Tuple
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject
from config.settings import settings

# In-memory storage: (user_id, action_type) -> list of timestamps
_rate_limit_records: Dict[Tuple[int, str], List[float]] = {}

RATE_LIMIT_CONFIG = {
    "create_offer": {"limit": 3, "window": 600},  # 3 operations per 10 mins (600s)
    "start_deal": {"limit": 5, "window": 600}      # 5 operations per 10 mins (600s)
}

def check_and_record_rate_limit(user_id: int, action_type: str) -> Tuple[bool, int]:
    """
    Checks if a user exceeded the rolling rate limit.
    Returns (is_allowed: bool, retry_after_seconds: int).
    If allowed, records the timestamp in memory.
    """
    if user_id in settings.ADMIN_IDS:
        return True, 0

    cfg = RATE_LIMIT_CONFIG.get(action_type)
    if not cfg:
        return True, 0

    now = time.time()
    key = (user_id, action_type)
    timestamps = _rate_limit_records.get(key, [])

    # Filter out timestamps older than the sliding window
    valid_timestamps = [ts for ts in timestamps if now - ts < cfg["window"]]

    if len(valid_timestamps) >= cfg["limit"]:
        oldest_ts = min(valid_timestamps)
        retry_after = int(cfg["window"] - (now - oldest_ts))
        _rate_limit_records[key] = valid_timestamps
        return False, max(1, retry_after)

    valid_timestamps.append(now)
    _rate_limit_records[key] = valid_timestamps
    return True, 0


class RateLimitMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)

        user_id = event.from_user.id if event.from_user else None
        if not user_id or user_id in settings.ADMIN_IDS:
            return await handler(event, data)

        cb_data = event.data or ""

        # Determine action_type based on specific trigger callback
        action_type = None
        if cb_data.startswith("start_deal_") or cb_data.startswith("start_neg_"):
            action_type = "start_deal"

        if action_type:
            is_allowed, retry_after = check_and_record_rate_limit(user_id, action_type)
            if not is_allowed:
                minutes = (retry_after + 59) // 60
                await event.answer(
                    f"⚠️ تجاوزت الحد المسموح لعمليات بدء الصفقات (حد زمني مؤقت لمنع التكرار).\n"
                    f"يرجى الانتظار حوالي {minutes} دقيقة والمحاولة مجدداً.",
                    show_alert=True
                )
                return

        return await handler(event, data)
