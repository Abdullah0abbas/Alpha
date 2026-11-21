"""Pure mathematical and formatting utilities for the Offer Creation Wizard.

Strictly zero dependencies on aiogram or sqlalchemy.
Provides navigation history management, completeness validation,
primary payout info extraction, and summary presentation formatting.
"""
import re
from typing import Tuple
from decimal import Decimal, InvalidOperation
from bot.utils.fees import format_fee_rule_line
from bot.utils.formatters import escape_markdown

FEE_RULE_LINE = f"{format_fee_rule_line()}"

MAX_OFFER_NOTE_LENGTH = 40

SHAM_CASH_NOTE_SUGGESTIONS = [
    "اسم ثلاثي حصرا",
    "حسابي موثق",
    "لا يقبل طرف ثالث",
    "اريد حساب شام كاش موثق حصرا ⛔⛔",
]

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

    # Note validation (optional, max 40 chars)
    note_val = wizard_data.get("note")
    if note_val is not None and len(str(note_val).strip()) > MAX_OFFER_NOTE_LENGTH:
        return False, f"الملاحظة يجب ألا تتجاوز {MAX_OFFER_NOTE_LENGTH} حرفاً."

    # Duration validation (5 minutes up to 4 hours)
    try:
        dur = float(wizard_data.get("duration_hours", 4.0))
        if dur < (5.0 / 60.0 - 0.001) or dur > 4.0:
            return False, "مدة صلاحية العرض يجب أن تكون بين 5 دقائق و 4 ساعات."
    except (TypeError, ValueError):
        return False, "مدة الصلاحية غير صالحة."

    return True, ""


def format_duration_hours_minutes(duration_hours: float | str | None) -> str:
    """Format duration in hours/minutes cleanly (e.g. '1 ساعة و 5 دقيقة', '30 دقيقة', '4 ساعات')."""
    try:
        total_mins = int(round(float(duration_hours or 4.0) * 60))
    except Exception:
        return "4 ساعات"

    if total_mins <= 0:
        return "4 ساعات"

    hours = total_mins // 60
    mins = total_mins % 60

    if hours > 0 and mins > 0:
        return f"{hours} ساعة و {mins} دقيقة"
    if hours > 0:
        return f"{hours} ساعات" if hours >= 3 else f"{hours} ساعة"
    return f"{mins} دقيقة"


def _fmt_rate_str(rate_val) -> str:
    try:
        dec = Decimal(str(rate_val))
        if dec == dec.to_integral_value():
            return f"{int(dec):,}"
        return f"{dec.normalize()}"
    except Exception:
        return str(rate_val)


def _fmt_usdt_str(val) -> str:
    try:
        dec = Decimal(str(val))
        return f"{dec:.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(val)


def format_wizard_draft_header(wizard_data: dict, up_to_step: str) -> str:
    """Builds a compact progressive header showing accumulated choices so far."""
    lines = ["➕ **إنشاء عرض جديد**"]
    offer_type = wizard_data.get("offer_type")
    if offer_type in ("sell", "buy"):
        type_ar = "🔴 بيع USDT" if offer_type == "sell" else "🟢 شراء USDT"
        lines.append(f"⚖️ **النوع :** {type_ar}")

    if up_to_step in ("min_limit", "max_limit", "methods", "payout", "negotiation", "note", "duration"):
        if wizard_data.get("rate"):
            lines.append(f"📊 **سعر الصرف :** `{_fmt_rate_str(wizard_data['rate'])}` ل.س")

    if up_to_step == "max_limit":
        if wizard_data.get("min_limit"):
            lines.append(f"💰 **الحد الأدنى :** `{_fmt_usdt_str(wizard_data['min_limit'])} USDT`")
    elif up_to_step in ("methods", "payout", "negotiation", "note", "duration"):
        if wizard_data.get("min_limit") and wizard_data.get("max_limit"):
            min_s = _fmt_usdt_str(wizard_data["min_limit"])
            max_s = _fmt_usdt_str(wizard_data["max_limit"])
            lines.append(f"💰 **الكمية :** من `{min_s}` إلى `{max_s} USDT`")

    if up_to_step in ("payout", "negotiation", "note", "duration"):
        methods = wizard_data.get("payment_methods") or []
        if methods:
            m_str = "، ".join(f"{PAYMENT_METHOD_ICONS.get(m, '💳')} {m}" for m in methods)
            lines.append(f"🏦 **طرق الدفع :** {m_str}")

    if up_to_step in ("note", "duration"):
        if "negotiable" in wizard_data:
            is_neg = wizard_data.get("negotiable", False)
            neg_type = wizard_data.get("negotiable_type", "none")
            neg_labels = {
                "amount": "متاح على الكمية",
                "rate": "متاح على السعر",
                "both": "متاح على السعر والكمية",
            }
            neg_s = neg_labels.get(neg_type, "متاح") if (is_neg and neg_type != "none") else "غير متاح 🔒"
            lines.append(f"🤝 **التفاوض :** {neg_s}")

    if up_to_step == "duration":
        note_raw = wizard_data.get("note")
        if note_raw:
            lines.append(f"📝 **الملاحظة :** {escape_markdown(str(note_raw))}")

    lines.append("〰️〰️〰️〰️〰️〰️〰️\n")
    return "\n".join(lines)


def format_wizard_summary(wizard_data: dict, bot_name: str = "Alpha Bot") -> str:
    """Format full, clean summary markdown for the offer owner before publication."""
    offer_type = wizard_data.get("offer_type", "")
    type_ar = "🔴 بيع USDT (استلام ليرة سورية)" if offer_type == "sell" else "🟢 شراء USDT (دفع ليرة سورية)"

    rate_str = _fmt_rate_str(wizard_data.get("rate", "0"))
    min_str = _fmt_usdt_str(wizard_data.get("min_limit", "0"))
    max_str = _fmt_usdt_str(wizard_data.get("max_limit", "0"))

    methods = wizard_data.get("payment_methods", []) or []
    formatted_methods = []
    for m in methods:
        icon = PAYMENT_METHOD_ICONS.get(m, "💳")
        formatted_methods.append(f"{icon} {m}")
    methods_str = "، ".join(formatted_methods) if formatted_methods else "غير محدد"

    payout_details = wizard_data.get("payout_details", {}) or {}
    payout_lines = []
    if offer_type == "sell":
        for m in methods:
            acc = str(payout_details.get(m, "غير محدد"))
            if acc.startswith(f"{m}: "):
                acc = acc[len(m) + 2:].strip()
            icon = PAYMENT_METHOD_ICONS.get(m, "💳")
            payout_lines.append(f"{icon} **{m}:** `{acc}`")
    else:
        wallet = payout_details.get("usdt_bep20_address", "غير محدد")
        payout_lines.append(f"📥 **محفظة الاستلام (BEP-20):**\n`{wallet}`")

    payout_section = "\n".join(payout_lines) if payout_lines else "غير مسجلة"

    is_neg = wizard_data.get("negotiable", False)
    neg_type = wizard_data.get("negotiable_type", "none")
    if is_neg and neg_type != "none":
        neg_labels = {
            "amount": "متاح على الكمية فقط",
            "rate": "متاح على السعر فقط",
            "both": "متاح على السعر والكمية معاً"
        }
        neg_str = neg_labels.get(neg_type, "متاح")
    else:
        neg_str = "غير متاح (شروط ثابتة)"

    note_val = wizard_data.get("note")
    is_custom_note = bool(wizard_data.get("is_custom_note", False))
    if note_val:
        custom_badge = " *(مخصصة — تتطلب موافقة الإدارة قبل النشر ⏳)*" if is_custom_note else ""
        note_str = f"{escape_markdown(str(note_val))}{custom_badge}"
    else:
        note_str = "لا يوجد"

    dur_val = wizard_data.get("duration_hours", 4.0)
    dur_str = format_duration_hours_minutes(dur_val)

    lines = [
        "📋 **المراجعة النهائية وتأكيد نشر العرض**",
        "〰️〰️〰️〰️〰️〰️〰️",
        f"⚖️ **النوع:** {type_ar}",
        f"📊 **سعر الصرف:** `{rate_str}` ل.س",
        f"💰 **الكمية:** من `{min_str} USDT` إلى `{max_str} USDT`",
        f"🏦 **طرق الدفع:** {methods_str}",
        f"🤝 **التفاوض:** {neg_str}",
        f"📝 **الملاحظة:** {note_str}",
        f"⏳ **مدة العرض:** {dur_str}",
        "〰️〰️〰️〰️〰️〰️〰️",
        "🔒 **بيانات الاستلام الخاصة (لا تظهر بالقناة):**",
        f"{payout_section}",
        "",
        f"{format_fee_rule_line()}",
    ]

    return "\n".join(lines)
