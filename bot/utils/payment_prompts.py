from typing import Optional
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.utils.formatters import PAYMENT_METHOD_ICONS
from bot.utils.validators import validate_payment_method_info


PAYMENT_METHOD_SPECS = {
    "sham_lira": {
        "name": "شام كاش ليرة",
        "icon": "💳",
        "instruction": "أرسل عنوان حساب شام كاش ليرة فقط، من دون أي نص إضافي (32 خانة hex):",
        "creation_instruction": "أرسل عنوان حساب شام كاش ليرة فقط، من دون أي نص إضافي (32 خانة hex):",
    },
    "syriatel": {
        "name": "سيريتيل كاش",
        "icon": "📱",
        "instruction": "أرسل رقم سيريتيل كاش فقط، من دون أي نص إضافي (10 أرقام يبدأ بـ 09):",
        "creation_instruction": "أرسل رقم سيريتيل كاش فقط، من دون أي نص إضافي (10 أرقام يبدأ بـ 09):",
    },
    "haram": {
        "name": "حوالة بنكية (الهرم)",
        "icon": "🏛️",
        "instruction": "أرسل الاسم الثلاثي والمحافظة ورقم الموبايل في سطر واحد:",
        "creation_instruction": "أرسل الاسم الثلاثي والمحافظة ورقم الموبايل في سطر واحد:",
    },
    "fouad": {
        "name": "حوالة بنكية (الفؤاد)",
        "icon": "🏢",
        "instruction": "أرسل الاسم الثلاثي والمحافظة ورقم الموبايل في سطر واحد:",
        "creation_instruction": "أرسل الاسم الثلاثي والمحافظة ورقم الموبايل في سطر واحد:",
    },
}

_NAME_TO_KEY = {spec["name"]: key for key, spec in PAYMENT_METHOD_SPECS.items()}

_KNOWN_PREFIXES = {
    "شام كاش ليرة",
    "شام كاش",
    "سيريتيل كاش",
    "سيريتل كاش",
    "حوالة بنكية (الهرم)",
    "حوالة بنكية (الفؤاد)",
    "حوالة بنكية",
}


def resolve_pm_key(method_name: Optional[str]) -> str:
    """Maps a payment method display name or legacy alias to its canonical key."""
    if not method_name:
        return "sham_lira"
    m = str(method_name).strip()
    if m in PAYMENT_METHOD_SPECS:
        return m
    if m in _NAME_TO_KEY:
        return _NAME_TO_KEY[m]
    if "شام كاش" in m:
        return "sham_lira"
    if "سيريتيل" in m or "سيريتل" in m:
        return "syriatel"
    if "الفؤاد" in m:
        return "fouad"
    if any(k in m for k in ("الهرم", "حوالة", "بنك")):
        return "haram"
    return "sham_lira"


def get_pm_spec(method_or_key: Optional[str]) -> dict:
    """Returns the canonical payment method spec (falls back to sham_lira)."""
    key = resolve_pm_key(method_or_key)
    return PAYMENT_METHOD_SPECS[key]


def extract_clean_account(raw_info: Optional[str], method_name: Optional[str] = None) -> str:
    """Strips redundant leading '<method>: ' label from stored payout account info."""
    if not raw_info:
        return ""
    text = str(raw_info).strip()
    if not text or text in ("PENDING", "غير محدد", "N/A"):
        return ""
    if ": " in text:
        prefix, rest = text.split(": ", 1)
        p_clean = prefix.strip()
        if p_clean in _KNOWN_PREFIXES or (method_name and p_clean == str(method_name).strip()):
            return rest.strip()
    return text


def match_saved_payment_info(
    saved_info: Optional[str],
    target_method: Optional[str] = None,
    allowed_methods: Optional[list[str]] = None,
) -> tuple[bool, Optional[str], str, str]:
    """
    Parses, validates, and checks compatibility of a user's saved profile payment info
    against a deal's locked target_method or an offer's allowed_methods list.

    Returns: (is_usable, canonical_method_name, clean_account_value, reason_ar)
    """
    if not saved_info:
        return False, None, "", "لا توجد بيانات دفع محفوظة في ملفك الشخصي."

    raw = str(saved_info).strip()
    if not raw or raw in ("PENDING", "غير محدد", "N/A"):
        return False, None, "", "لا توجد بيانات دفع محفوظة في ملفك الشخصي."

    detected_method: Optional[str] = None
    candidate_account = raw
    if ": " in raw:
        prefix, rest = raw.split(": ", 1)
        p_clean = prefix.strip()
        if p_clean in _KNOWN_PREFIXES or p_clean in PAYMENT_METHOD_SPECS:
            detected_method = get_pm_spec(p_clean)["name"]
            candidate_account = rest.strip()

    has_target = bool(target_method and str(target_method).strip() not in ("", "غير محدد", "PENDING"))
    validation_method = detected_method or (get_pm_spec(target_method)["name"] if has_target else None)

    is_valid, formatted_info, err_msg = validate_payment_method_info(validation_method, candidate_account)
    if not is_valid:
        return False, detected_method, "", err_msg

    # Resolve canonical method name from formatted prefix if auto-detected
    resolved_method = detected_method
    if not resolved_method:
        if ": " in formatted_info:
            fmt_prefix = formatted_info.split(": ", 1)[0].strip()
            resolved_method = get_pm_spec(fmt_prefix)["name"]
        else:
            resolved_method = get_pm_spec(target_method)["name"]

    clean_account = extract_clean_account(formatted_info, resolved_method)

    if has_target:
        canonical_target = get_pm_spec(target_method)["name"]
        if resolve_pm_key(resolved_method) != resolve_pm_key(canonical_target):
            return (
                False,
                resolved_method,
                clean_account,
                f"حسابك المحفوظ ({resolved_method}) لا يطابق وسيلة الدفع المحددة في الصفقة ({canonical_target}).",
            )
    elif allowed_methods:
        allowed_keys = {resolve_pm_key(m) for m in allowed_methods}
        if resolve_pm_key(resolved_method) not in allowed_keys:
            return (
                False,
                resolved_method,
                clean_account,
                f"حسابك المحفوظ ({resolved_method}) غير ضمن وسائل الدفع المقبولة لهذا العرض.",
            )

    return True, resolved_method, clean_account, ""


def build_profile_pm_prompt(pm_key: str) -> Optional[tuple[str, str]]:
    """
    Returns (method_name, markdown_prompt) for profile payment setup if pm_key is valid,
    or None if pm_key is unknown.
    """
    spec = PAYMENT_METHOD_SPECS.get(pm_key)
    if not spec:
        return None
    prompt = f"{spec['icon']} **{spec['name']}**\n{spec['instruction']}"
    return spec["name"], prompt


def build_deal_pm_prompt(method_or_key: Optional[str], deal_id: int, *, strict: bool = False) -> Optional[tuple[str, str]]:
    """
    Returns (method_name, markdown_prompt) for updating/setting seller payment info in a deal.
    When strict=True, returns None if method_or_key is not an exact canonical key.
    """
    if strict:
        spec = PAYMENT_METHOD_SPECS.get(method_or_key or "")
        if not spec:
            return None
    else:
        spec = get_pm_spec(method_or_key)

    prompt = (
        f"{spec['icon']} **صفقة #{deal_id} — {spec['name']}**\n"
        f"{spec['instruction']}"
    )
    return spec["name"], prompt


def build_seller_pm_creation_prompt(selected_pm: str, offer_id: Optional[int]) -> str:
    """Builds the seller payout prompt shown when accepting a Buy USDT offer."""
    icon = PAYMENT_METHOD_ICONS.get(selected_pm, "💳")
    offer_label = f" | عرض #{offer_id}" if offer_id else ""

    if selected_pm in _NAME_TO_KEY or "شام كاش" in selected_pm or "سيريتيل" in selected_pm or "سيريتل" in selected_pm or any(
        k in selected_pm for k in ("حوالة", "الهرم", "الفؤاد", "بنك")
    ):
        details = get_pm_spec(selected_pm)["creation_instruction"]
    else:
        details = f"أرسل رقم الحساب لوسيلة ({selected_pm}) فقط، من دون أي نص إضافي:"

    return (
        f"{icon} **{selected_pm}{offer_label}**\n"
        f"{details}"
    )


def build_payment_method_selection_kb(
    callback_prefix: str,
    back_text: str,
    back_callback: str,
    callback_suffix: str = "",
) -> InlineKeyboardMarkup:
    """Builds the standard 4-method payment selection inline keyboard."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💳 شام كاش ليرة",
                callback_data=f"{callback_prefix}sham_lira{callback_suffix}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="📱 سيريتيل كاش",
                callback_data=f"{callback_prefix}syriatel{callback_suffix}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🏛️ حوالة بنكية (الهرم)",
                callback_data=f"{callback_prefix}haram{callback_suffix}",
            ),
            InlineKeyboardButton(
                text="🏢 حوالة بنكية (الفؤاد)",
                callback_data=f"{callback_prefix}fouad{callback_suffix}",
            ),
        ],
        [
            InlineKeyboardButton(text=back_text, callback_data=back_callback),
        ],
    ])


def build_allowed_pm_selection_kb(
    allowed_methods: Optional[list[str]],
    callback_prefix: str,
    back_text: str,
    back_callback: str,
    callback_suffix: str = "",
) -> InlineKeyboardMarkup:
    """
    Builds a payment method selection keyboard restricted to allowed_methods when provided,
    falling back to all 4 canonical methods if allowed_methods is empty/None.
    """
    if not allowed_methods:
        return build_payment_method_selection_kb(
            callback_prefix=callback_prefix,
            back_text=back_text,
            back_callback=back_callback,
            callback_suffix=callback_suffix,
        )

    rows = []
    seen_keys: set[str] = set()
    for method in allowed_methods:
        key = resolve_pm_key(method)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        spec = PAYMENT_METHOD_SPECS[key]
        rows.append([
            InlineKeyboardButton(
                text=f"{spec['icon']} {spec['name']}",
                callback_data=f"{callback_prefix}{key}{callback_suffix}",
            )
        ])

    rows.append([InlineKeyboardButton(text=back_text, callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def resolve_deal_target_pm(deal, offer_methods: Optional[list[str]] = None) -> str:
    """Resolves the target payment method name for a deal from seller_payment_info or offer."""
    info = getattr(deal, "seller_payment_info", None)
    if isinstance(info, dict):
        method = info.get("method")
        if method and method not in ("غير محدد", "PENDING"):
            return str(method)
    if offer_methods:
        return str(offer_methods[0])
    return "شام كاش ليرة"
