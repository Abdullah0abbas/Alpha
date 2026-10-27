"""Pure mathematical and formatting utilities for the Offer Creation Wizard.

Strictly zero dependencies on aiogram or sqlalchemy.
Provides navigation history management, completeness validation,
primary payout info extraction, and summary presentation formatting.
"""
from bot.utils.fees import format_fee_rule_line

FEE_RULE_LINE = f"⚡ **عمولة المنصة:** {format_fee_rule_line().replace('⚡ العمولة: ', '')}"

PAYMENT_METHOD_ICONS = {
    "شام كاش ليرة": "💳",
    "سيريتيل كاش": "📱",
    "حوالة بنكية (الهرم)": "🏛️",
    "حوالة بنكية (الفؤاد)": "🏢",
}


def push_wizard_step(history: list[dict], step_name: str, snapshot_data: dict) -> list[dict]:
    """Append a step snapshot to the wizard navigation history."""
    new_history = list(history or [])
    # Store clean copy of relevant snapshot
    record = {
        "step": step_name,
        "data": dict(snapshot_data)
    }
    new_history.append(record)
    return new_history


def pop_wizard_step(history: list[dict]) -> Tuple[dict | None, list[dict]]:
    """Pop and return the previous step record and updated history list."""
    if not history:
        return None, []
    new_history = list(history)
    prev = new_history.pop()
    return prev, new_history


def extract_primary_payout_info(wizard_data: dict) -> str:
    """Extract primary payout info string for backward compatibility with Offer.payout_info."""
    offer_type = wizard_data.get("offer_type", "")
    payout_details = wizard_data.get("payout_details", {}) or {}

    if offer_type == "buy":
        return str(payout_details.get("usdt_bep20_address") or payout_details.get("wallet") or "").strip()

    # In sell offers, return the first available payment method account
    methods = wizard_data.get("payment_methods", []) or []
    for m in methods:
        val = payout_details.get(m)
        if val:
            return f"{m}: {val}" if not str(val).startswith(m) else str(val)

    # Fallback to any string value in payout_details
    for k, v in payout_details.items():
        if v:
            return f"{k}: {v}"
    return "PENDING"


def is_wizard_data_complete(wizard_data: dict) -> Tuple[bool, str]:
    """Validate completeness and integrity of wizard data before publishing."""
    offer_type = wizard_data.get("offer_type")
    if offer_type not in ("sell", "buy"):
        return False, "نوع العملية غير محدد."

    # Rate validation
    try:
        rate = Decimal(str(wizard_data.get("rate", "0")))
        if rate <= Decimal("0"):
            return False, "سعر الصرف يجب أن يكون رقماً موجباً."
    except (InvalidOperation, TypeError, ValueError):
        return False, "سعر الصرف غير صالح."

    # Limits validation
    try:
        min_limit = Decimal(str(wizard_data.get("min_limit", "0")))
        max_limit = Decimal(str(wizard_data.get("max_limit", "0")))
        if min_limit <= Decimal("0"):
            return False, "الحد الأدنى يجب أن يكون أكبر من الصفر."
        if max_limit < min_limit:
            return False, "الحد الأقصى يجب أن يكون أكبر من أو يساوي الحد الأدنى."
    except (InvalidOperation, TypeError, ValueError):
        return False, "حدود الكمية غير صالحة."

    # Payment methods validation
    methods = wizard_data.get("payment_methods", [])
    if not isinstance(methods, list) or not methods:
        return False, "يجب تحديد وسيلة دفع واحدة على الأقل."

    if "شام كاش دولار" in methods:
        return False, "وسيلة الدفع شام كاش دولار لم تعد مدعومة."

    # Payout details validation
    payout_details = wizard_data.get("payout_details", {})
    if not isinstance(payout_details, dict):
        return False, "بيانات الاستلام غير صالحة."

    if offer_type == "sell":
        for m in methods:
            acc = str(payout_details.get(m, "")).strip()
            if not acc:
                return False, f"بيانات حساب الاستلام لوسيلة '{m}' مفقودة."
    elif offer_type == "buy":
        wallet = str(payout_details.get("usdt_bep20_address", "")).strip()
        if not wallet or not re.match(r"^0[xX][0-9a-fA-F]{40}$", wallet):
            return False, "عنوان محفظة استلام USDT (BEP-20) غير صالح."

    # Duration validation
    try:
        dur = float(wizard_data.get("duration_hours", 4.0))
        if dur < 1.0 or dur > 4.0:
            return False, "مدة صلاحية العرض يجب أن تكون بين 1 و 4 ساعات."
    except (TypeError, ValueError):
        return False, "مدة الصلاحية غير صالحة."

    return True, ""


def format_wizard_summary(wizard_data: dict, bot_name: str = "Alpha Bot") -> str:
    """Format full, rich summary markdown for the offer owner before publication."""
    offer_type = wizard_data.get("offer_type", "")
    type_ar = "🔴 بيع USDT (استلام ليرة سورية)" if offer_type == "sell" else "🟢 شراء USDT (دفع ليرة سورية)"

    rate_val = wizard_data.get("rate", "0")
    try:
        rate_int = int(round(Decimal(str(rate_val))))
        rate_str = f"{rate_int:,}"
    except Exception:
        rate_str = str(rate_val)

    min_val = wizard_data.get("min_limit", "0")
    max_val = wizard_data.get("max_limit", "0")
    try:
        min_dec = Decimal(str(min_val))
        min_str = f"{min_dec:.2f}".rstrip('0').rstrip('.')
    except Exception:
        min_str = str(min_val)
    try:
        max_dec = Decimal(str(max_val))
        max_str = f"{max_dec:.2f}".rstrip('0').rstrip('.')
    except Exception:
        max_str = str(max_val)

    # Payment methods
    methods = wizard_data.get("payment_methods", []) or []
    formatted_methods = []
    for m in methods:
        icon = PAYMENT_METHOD_ICONS.get(m, "💳")
        formatted_methods.append(f"{icon} {m}")
    methods_str = "، ".join(formatted_methods) if formatted_methods else "غير محدد"

    # Payout details
    payout_details = wizard_data.get("payout_details", {}) or {}
    payout_lines = []
    if offer_type == "sell":
        for m in methods:
            acc = payout_details.get(m, "لم يُحدد")
            icon = PAYMENT_METHOD_ICONS.get(m, "💳")
            payout_lines.append(f"  • {icon} **{m}:** `{acc}`")
    else:
        wallet = payout_details.get("usdt_bep20_address", "لم يُحدد")
        payout_lines.append(f"  • 📥 **محفظة استلام USDT (BEP-20):** `{wallet}`")

    payout_section = "\n".join(payout_lines) if payout_lines else "  • غير مسجلة"

    # Negotiation
    is_neg = wizard_data.get("negotiable", False)
    neg_type = wizard_data.get("negotiable_type", "none")
    if is_neg and neg_type != "none":
        neg_labels = {
            "amount": "متاح على الكمية فقط 📦",
            "rate": "متاح على السعر فقط 💵",
            "both": "متاح على السعر والكمية معاً ⚖️"
        }
        neg_str = neg_labels.get(neg_type, "متاح 🤝")
    else:
        neg_str = "شروط ثابتة فقط (غير متاح للتفاوض) 🔒"

    # Duration
    dur_val = wizard_data.get("duration_hours", 4.0)
    try:
        dur_float = float(dur_val)
        dur_str = f"{int(dur_float)} ساعات" if dur_float == int(dur_float) else f"{dur_float} ساعة"
    except Exception:
        dur_str = f"{dur_val} ساعات"

    lines = [
        f"📋 **المراجعة النهائية وتأكيد نشر العرض**",
        "━━━━━━━━━━━━━━━",
        f"• **نوع العملية:** {type_ar}",
        f"• **سعر الصرف:** 💵 `{rate_str} SYP` لكل 1 USDT",
        f"• **حدود الكمية:** 📦 من `{min_str}` إلى `{max_str}` USDT",
        f"• **وسائل الدفع المقبولة:** {methods_str}",
        "",
        "🔒 **بيانات الاستلام الخاصة بك (لن تظهر علناً في القناة):**",
        f"{payout_section}",
        "",
        f"• **التفاوض:** {neg_str}",
        f"• **صلاحية العرض:** ⏱️ {dur_str}",
        f"• **العمولة:** {FEE_RULE_LINE}",
        "━━━━━━━━━━━━━━━",
        "👇 اضغط على «تأكيد ونشر العرض» لطرحه في السوق، أو اختر أي قسم لتعديله مباشرة:"
    ]

    return "\n".join(lines)
