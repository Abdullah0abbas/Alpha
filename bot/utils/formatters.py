"""
Formatting and Presentation utilities for Clean, Professional, and Accessible UI.
Focuses on visual whitespace, elegant typography, humanized durations, and readable numbers.
"""
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone, timedelta
from config.settings import settings

MIN_DEAL_AMOUNT_USDT = Decimal("1")

def to_local(dt: datetime | None, offset_hours: int | None = None) -> datetime | None:
    """
    Converts a datetime to local timezone (Damascus fixed offset UTC+3 by default).
    Naive datetime is treated as UTC.
    Uses fixed timezone(timedelta(hours=...)) without zoneinfo or tzdata dependencies.
    """
    if dt is None:
        return None
    if offset_hours is None:
        offset_hours = getattr(settings, "DISPLAY_UTC_OFFSET_HOURS", 3)
    tz = timezone(timedelta(hours=offset_hours))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)

def format_local_datetime(dt: datetime | None, offset_hours: int | None = None, include_suffix: bool = True) -> str:
    """
    Formats a datetime as 'YYYY-MM-DD — HH:MM' with optional ' (بتوقيت دمشق)' suffix.
    """
    if not dt:
        return "غير محدد"
    local_dt = to_local(dt, offset_hours)
    if not local_dt:
        return "غير محدد"
    formatted = local_dt.strftime("%Y-%m-%d — %H:%M")
    if include_suffix:
        formatted = f"{formatted} (بتوقيت دمشق)"
    return formatted

def format_local_time(dt: datetime | None, offset_hours: int | None = None) -> str:
    """
    Formats time portion as 'HH:MM'.
    """
    if not dt:
        return "غير محدد"
    local_dt = to_local(dt, offset_hours)
    if not local_dt:
        return "غير محدد"
    return local_dt.strftime("%H:%M")

ARABIC_EASTERN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

def clean_numeric_input(raw_text: str | None) -> Decimal | None:
    """
    Intelligently cleans and parses user-entered amounts from Telegram chats:
    - Converts Eastern Arabic (٠-٩) and Persian (۰-۹) numerals to standard ASCII.
    - Strips currency keywords (USDT, SYP, $, ل.س, ليرة, دولار, etc.).
    - Removes zero-width and directional formatting chars (RTL/LTR marks).
    - Normalizes thousand separators and decimal points (e.g., 100,5 -> 100.5, 1,000,000 -> 1000000).
    - Returns Decimal or None if invalid or non-positive.
    """
    if not raw_text:
        return None

    # 1. Translate Arabic/Persian digits
    text = str(raw_text).translate(ARABIC_EASTERN_DIGITS)

    # 2. Remove invisible unicode formatting marks
    text = re.sub(r'[\u200e\u200f\u202a-\u202e\u200b-\u200d\ufeff]', '', text)

    # 3. Strip common currency words and symbols
    text = re.sub(r'(?i)(usdt|syp|usd|\$|ل\.س|ليرة|دولار|لير|سوري)', '', text)

    # 4. Strip whitespace
    text = text.strip()

    # Replace Arabic comma '،' with ','
    text = text.replace('،', ',')

    if ',' in text and '.' in text:
        if text.rfind(',') > text.rfind('.'):
            # European format e.g. 1.000,50
            text = text.replace('.', '').replace(',', '.')
        else:
            # Standard US format e.g. 1,000.50
            text = text.replace(',', '')
    elif ',' in text:
        parts = text.split(',')
        if len(parts) == 2 and len(parts[1]) in (1, 2) and parts[1].isdigit():
            # Decimal point e.g. "100,5" or "100,25"
            text = f"{parts[0]}.{parts[1]}"
        elif all(len(p) == 3 for p in parts[1:]) and all(p.isdigit() for p in parts[1:]):
            # Thousand separator e.g. "100,000" or "1,000,000"
            text = "".join(parts)
        else:
            text = text.replace(',', '.')

    # 5. Extract only valid numeric and dot characters
    cleaned = re.sub(r'[^\d.]', '', text)
    if not cleaned or cleaned.count('.') > 1:
        return None

    try:
        val = Decimal(cleaned)
        if val <= Decimal("0"):
            return None
        return val
    except (InvalidOperation, Exception):
        return None

def format_syp(amount) -> str:
    """Format SYP amount cleanly with up to 2 decimals if needed and thousand separators for New Syrian Lira scale."""
    try:
        val = Decimal(str(amount))
        formatted = f"{val:,.2f}".rstrip('0').rstrip('.')
        return formatted if formatted else "0"
    except Exception:
        return str(amount)

def format_usdt(amount) -> str:
    """Format USDT amount cleanly with up to 2 decimals, trimming trailing zeros."""
    try:
        val = Decimal(str(amount))
        formatted = f"{val:.2f}".rstrip('0').rstrip('.')
        return formatted if formatted else "0"
    except Exception:
        return str(amount)

AVAILABLE_PAYMENT_METHODS = [
    "شام كاش ليرة",
    "سيريتيل كاش",
    "حوالة بنكية (الهرم)",
    "حوالة بنكية (الفؤاد)",
]

PAYMENT_METHOD_ICONS = {
    "شام كاش ليرة": "💳",
    "شام كاش دولار": "💵",
    "سيريتيل كاش": "📱",
    "حوالة بنكية (الهرم)": "🏛️",
    "حوالة بنكية (الفؤاد)": "🏢",
    # Legacy aliases for backwards compatibility with pre-existing offers
    "شام كاش": "💳",
    "سيريتل كاش": "📱",
    "بيمو": "🏦",
    "كاش": "💵",
    "تحويل بنكي": "🏛️",
}

def format_payment_methods(methods: list[str] | None) -> str:
    if not methods:
        return "غير محدد"
    formatted = []
    for m in methods:
        m_str = str(m).strip()
        icon = PAYMENT_METHOD_ICONS.get(m_str, "💳")
        if m_str.startswith(icon):
            formatted.append(m_str)
        else:
            formatted.append(f"{icon} {m_str}")
    return "، ".join(formatted)

def humanize_time_remaining(expires_at: datetime | None, default_str: str = "غير محدد") -> str:
    """
    Returns a clean, human-friendly duration like '3 ساعات و 45 دقيقة' or '14 دقيقة'
    instead of raw UTC timestamps.
    """
    if not expires_at:
        return default_str
        
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
        
    diff = expires_at - now
    total_seconds = int(diff.total_seconds())
    
    if total_seconds <= 0:
        return "منتهية"
        
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    
    if hours > 0:
        if minutes > 0:
            return f"{hours} ساعة و {minutes} دقيقة"
        return f"{hours} ساعة"
    elif minutes > 0:
        return f"{minutes} دقيقة"
    else:
        return "أقل من دقيقة"

def escape_markdown(text: str | None, version: int = 1) -> str:
    """
    Safely escapes characters with special meaning in Telegram Markdown formatting.
    Prevents 'Can't parse entities' errors when user-submitted text contains characters
    like '_', '*', '`', '[', ']', '(', ')', etc.
    """
    if not text:
        return ""
    text_str = str(text)
    if version == 1:
        # Standard Telegram legacy Markdown escapes
        escape_chars = r'_*`['
        return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text_str)
    else:
        # MarkdownV2 escapes
        escape_chars = r'_*[]()~`>#+-=|{}.!'
        return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text_str)

def safe_username(username: str | None, user_id: int | None = None) -> str:
    """
    Returns a markdown-safe username or ID representation:
    e.g., '@abdullah\\_abbas' or '123456789'
    """
    if username:
        escaped_user = escape_markdown(username.lstrip('@'))
        return f"@{escaped_user}"
    elif user_id:
        return f"`{user_id}`"
    return "مستخدم غير متاح"


def _ensure_inline_button_style_support() -> None:
    """Ensures aiogram 3 InlineKeyboardButton serializes the Telegram Bot API 9.4+ `style` property across all versions."""
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        if "style" not in InlineKeyboardButton.model_fields:
            InlineKeyboardButton.model_config["extra"] = "allow"
            InlineKeyboardButton.model_rebuild(force=True)
            InlineKeyboardMarkup.model_rebuild(force=True)
    except Exception:
        pass


_ensure_inline_button_style_support()


def styled_button(*, text: str, style: str | None = None, **kwargs):
    """
    Creates an InlineKeyboardButton with optional Telegram Bot API 9.4+ `style` ('danger', 'success', 'primary').
    Falls back gracefully if the underlying environment rejects extra fields.
    """
    from aiogram.types import InlineKeyboardButton

    if style:
        try:
            return InlineKeyboardButton(text=text, style=style, **kwargs)
        except Exception:
            return InlineKeyboardButton(text=text, **kwargs)
    return InlineKeyboardButton(text=text, **kwargs)


