import re
from typing import Tuple, Optional

try:
    from web3 import Web3
except ImportError:
    Web3 = None

HEX_PATTERN = re.compile(r"^0[xX][0-9a-fA-F]{40}$")

INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u200e', '\u200f',
    '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
    '\u2066', '\u2067', '\u2068', '\u2069'
}

BLACKLISTED_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0x0000000000000000000000000000000000000001",
    "0xffffffffffffffffffffffffffffffffffffffff",
}


def _pure_keccak256(data: bytes) -> bytes:
    """Pure Python Keccak-256 fallback when web3 library is unavailable."""
    RC = [
        0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
        0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
        0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
        0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
        0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
        0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008
    ]
    ROTC = [
        [ 0, 36,  3, 41, 18],
        [ 1, 44, 10, 45,  2],
        [62,  6, 43, 15, 61],
        [28, 55, 25, 21, 56],
        [27, 20, 39,  8, 14]
    ]
    state = [[0] * 5 for _ in range(5)]
    rate = 136
    d = bytearray(data)
    d.append(0x01)
    while len(d) % rate != (rate - 1):
        d.append(0x00)
    d.append(0x80)

    for offset in range(0, len(d), rate):
        block = d[offset:offset + rate]
        for i in range(17):
            val = int.from_bytes(block[i * 8:(i + 1) * 8], "little")
            x, y = i % 5, i // 5
            state[x][y] ^= val
        for round_idx in range(24):
            C = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
            D = [C[(x - 1) % 5] ^ (((C[(x + 1) % 5] << 1) & 0xffffffffffffffff) | (C[(x + 1) % 5] >> 63)) for x in range(5)]
            for x in range(5):
                for y in range(5):
                    state[x][y] ^= D[x]
            B = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    r = ROTC[x][y]
                    B[y][(2 * x + 3 * y) % 5] = ((state[x][y] << r) & 0xffffffffffffffff) | (state[x][y] >> (64 - r)) if r else state[x][y]
            for x in range(5):
                for y in range(5):
                    state[x][y] = B[x][y] ^ ((~B[(x + 1) % 5][y]) & B[(x + 2) % 5][y])
            state[0][0] ^= RC[round_idx]

    out = bytearray()
    for y in range(5):
        for x in range(5):
            out.extend(state[x][y].to_bytes(8, "little"))
    return bytes(out[:32])


def _to_checksum_address(val: str) -> str:
    if Web3 is not None:
        return Web3.to_checksum_address(val)
    addr = val[2:].lower()
    h = _pure_keccak256(addr.encode("ascii")).hex()
    checksummed = "".join(c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(addr))
    return "0x" + checksummed


def _is_checksum_address(val: str) -> bool:
    if Web3 is not None:
        return Web3.is_checksum_address(val)
    if not HEX_PATTERN.match(val):
        return False
    return val == _to_checksum_address(val)


def _is_address(val: str) -> bool:
    if Web3 is not None:
        return Web3.is_address(val)
    return bool(HEX_PATTERN.match(val))


def validate_bep20_address(raw: str | None) -> Tuple[bool, str, str]:
    """
    Validates and normalizes a BEP-20 / EVM wallet address according to EIP-55.
    Returns: (is_valid, normalized_address, error_message_ar)
    """
    if raw is None:
        return False, "", "يرجى إدخال عنوان محفظة غير فارغ."

    cleaned = str(raw).strip()
    for ch in INVISIBLE_CHARS:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.strip()

    if not cleaned:
        return False, "", "يرجى إدخال عنوان محفظة غير فارغ."

    if not HEX_PATTERN.match(cleaned):
        return (
            False,
            "",
            "⚠️ **عنوان المحفظة غير صالح!**\n\n"
            "يجب أن يبدأ بـ 0x ويتكون تماماً من 42 خانة بنظام الست عشري (Hexadecimal)."
        )

    cleaned_lower = "0x" + cleaned[2:].lower()

    if cleaned_lower in BLACKLISTED_ADDRESSES:
        return False, "", "هذا العنوان محظور (عنوان حرق أو عنوان صفري غير صالح للاستلام)."

    # EIP-55 Validation
    is_all_lower = (cleaned == cleaned.lower())
    is_all_upper = (cleaned[2:] == cleaned[2:].upper())

    if is_all_lower or is_all_upper:
        # Accept and normalize
        normalized = _to_checksum_address(cleaned_lower)
        return True, normalized, ""

    # Mixed case: must match checksum exactly
    if not _is_checksum_address(cleaned):
        return (
            False,
            "",
            "⚠️ **خطأ في التحقق من العنوان!**\n\n"
            "العنوان يحتوي أحرفاً كبيرة وصغيرة غير مطابقة لـ EIP-55 checksum. يرجى التأكد من نسخه بدقة دون تعديل."
        )

    return True, cleaned, ""


SHAM_CASH_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
SYRIATEL_PHONE_PATTERN = re.compile(r"^09\d{8}$")
_ARABIC_DIGITS_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def validate_sham_cash_address(raw: str | None) -> Tuple[bool, str, str]:
    """
    Validates Sham Cash address (32-character hexadecimal account ID).
    """
    if not raw:
        return False, "", "يرجى إدخال عنوان حساب شام كاش (32 خانة)."
    cleaned = str(raw).strip()
    for ch in INVISIBLE_CHARS:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.strip()

    # Strip common prefix labels (e.g. "شام كاش ليرة:", "شام كاش:", "حساب:", "العنوان:", "cham cash:")
    cleaned = re.sub(
        r"^(?:(?:شام\s*كاش(?:\s*(?:ليرة|دولار))?)|حساب|العنوان|cham\s*cash)[\s:\-—]*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    if not SHAM_CASH_HEX_PATTERN.match(cleaned):
        return (
            False,
            "",
            "⚠️ **عنوان حساب شام كاش غير صحيح!**\n\n"
            "يجب أن يتكون العنوان تماماً من 32 خانة (أرقام وأحرف إنجليزية دون مسافات)."
        )
    return True, cleaned.lower(), ""


def validate_syriatel_cash_number(raw: str | None) -> Tuple[bool, str, str]:
    """
    Validates Syriatel Cash Syrian mobile number (10 digits starting with 09).
    """
    if not raw:
        return False, "", "يرجى إدخال رقم سيريتيل كاش (10 أرقام يبدأ بـ 09)."
    cleaned = str(raw).strip()
    for ch in INVISIBLE_CHARS:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.translate(_ARABIC_DIGITS_MAP)
    cleaned = re.sub(
        r"^(?:سيريت[يي]ل\s*كاش|رقم|الهاتف|الموبايل)[\s:\-—]*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"[\s\-\.\(\)]+", "", cleaned)

    # Normalize international Syrian prefixes: +9639..., 009639..., 9639... -> 09...
    if cleaned.startswith("+9639"):
        cleaned = "09" + cleaned[5:]
    elif cleaned.startswith("009639"):
        cleaned = "09" + cleaned[6:]
    elif cleaned.startswith("9639"):
        cleaned = "09" + cleaned[4:]

    if not SYRIATEL_PHONE_PATTERN.match(cleaned):
        return (
            False,
            "",
            "⚠️ **رقم سيريتيل كاش غير صحيح!**\n\n"
            "يجب أن يكون رقماً سورياً مكوناً من 10 أرقام ويبدأ بـ 09."
        )
    return True, cleaned, ""


def validate_bank_transfer_info(raw: str | None) -> Tuple[bool, str, str]:
    """
    Validates bank / remittance transfer info (e.g. Al-Haram, Al-Fouad).
    Requires: Triple full name (at least 3 alphabetic words) and Syrian mobile number (09xxxxxxxx).
    """
    if not raw:
        return False, "", "يرجى إدخال الاسم الثلاثي ورقم الموبايل السوري."
    cleaned = str(raw).strip()
    for ch in INVISIBLE_CHARS:
        cleaned = cleaned.replace(ch, "")
    normalized = cleaned.translate(_ARABIC_DIGITS_MAP)

    # Strip leading method prefix only if followed by ':' (e.g. "حوالة بنكية (الفؤاد): ...")
    normalized = re.sub(
        r"^\s*(?:حوالة\s*بنكية\s*\((?:الهرم|الفؤاد)\)|حوالة\s*(?:الهرم|الفؤاد)|حوالة\s*بنكية)\s*:\s*",
        "",
        normalized,
    )

    # 1. Look for a Syrian phone number within the text
    phone_match = re.search(r"(?:(?:\+|00)?963[\s\-]*9|09)\d(?:[\s\-]?\d){7}(?!\d)", normalized)
    if not phone_match:
        return (
            False,
            "",
            "⚠️ **رقم الموبايل غير موجود أو غير صحيح!**\n\n"
            "يجب إدخال **الاسم الثلاثي** مع **رقم موبايل سوري صحيح** مكون من 10 أرقام يبدأ بـ `09`.\n"
            "مثال: `محمد أحمد العلي - 0912345678`"
        )

    raw_phone = phone_match.group(0)
    is_valid_phone, norm_phone, _ = validate_syriatel_cash_number(raw_phone)
    if not is_valid_phone:
        return (
            False,
            "",
            "⚠️ **رقم الموبايل غير صحيح!**\n\n"
            "يجب أن يتكون رقم الموبايل من 10 أرقام ويبدأ بـ `09`.\n"
            "مثال: `محمد أحمد العلي - 0912345678`"
        )

    # 2. Extract name part by removing the matched phone span
    name_part = normalized[:phone_match.start()] + " " + normalized[phone_match.end():]
    # Remove optional labels only when followed by ':' or '-' so real names like "فؤاد" or "الفؤاد" are preserved
    name_part = re.sub(
        r"(?:الاسم\s*الثلاثي|الاسم\s*الكامل|الاسم|رقم\s*الموبايل|رقم\s*الهاتف|الموبايل|الهاتف|جوال|موبايل)\s*[:\-—]+",
        " ",
        name_part,
    )
    name_part = re.sub(r"[,،/\\|_\-—:()\[\]]+", " ", name_part)
    name_words = [
        w for w in name_part.split()
        if re.fullmatch(r"[\u0600-\u06FFa-zA-Z]{2,}", w)
    ]

    if len(name_words) < 3:
        return (
            False,
            "",
            "⚠️ **الاسم الثلاثي غير مكتمل!**\n\n"
            "يجب كتابة **الاسم الثلاثي الكامل** (الاسم الأول + اسم الأب + الكنية) مع رقم الموبايل.\n"
            "مثال: `محمد أحمد العلي - 0912345678`"
        )

    full_name = " ".join(name_words)
    if len(full_name) > 80:
        return False, "", "⚠️ **الاسم المدخل طويل جداً!** يرجى الاكتفاء بالاسم الثلاثي ورقم الموبايل."

    formatted = f"{full_name} - {norm_phone}"
    return True, formatted, ""


def validate_payment_method_info(method: Optional[str], raw: str | None) -> Tuple[bool, str, str]:
    """
    Unified dispatcher validating payment details according to method type:
    - Sham Cash (Lira / Dollar): 32-character hex account.
    - Syriatel Cash: 10-digit Syrian mobile phone.
    - Bank Transfer (Al-Haram / Al-Fouad): Full triple name + mobile number.
    Returns: (is_valid, formatted_info, error_message_ar)
    """
    if raw is None:
        return False, "", "يرجى إدخال بيانات صالحة وغير فارغة."

    m = (method or "").strip()

    if "شام كاش" in m:
        valid, val, err = validate_sham_cash_address(raw)
        if not valid:
            return False, "", err
        prefix = m if m else "شام كاش"
        return True, f"{prefix}: {val}", ""

    elif "سيريتيل" in m or "سيريتل" in m:
        valid, val, err = validate_syriatel_cash_number(raw)
        if not valid:
            return False, "", err
        prefix = m if m else "سيريتيل كاش"
        return True, f"{prefix}: {val}", ""

    elif any(k in m for k in ("حوالة", "الهرم", "الفؤاد", "بنك")):
        valid, val, err = validate_bank_transfer_info(raw)
        if not valid:
            return False, "", err
        prefix = m if m else "حوالة بنكية"
        return True, f"{prefix}: {val}", ""

    # Auto-detection if method is generic or None
    v_sham, val_sham, _ = validate_sham_cash_address(raw)
    if v_sham:
        return True, f"شام كاش: {val_sham}", ""

    v_syr, val_syr, _ = validate_syriatel_cash_number(raw)
    if v_syr:
        return True, f"سيريتيل كاش: {val_syr}", ""

    v_bank, val_bank, _ = validate_bank_transfer_info(raw)
    if v_bank:
        return True, f"حوالة بنكية: {val_bank}", ""

    return (
        False,
        "",
        "⚠️ **البيانات المدخلة غير متوافقة مع وسائل الدفع المعتمدة!**\n\n"
        "• شام كاش: عنوان الحساب المكون من 32 خانة hex دون مسافات\n"
        "• سيريتيل كاش: رقم سوري 10 أرقام يبدأ بـ 09\n"
        "• حوالة بنكية (الهرم/الفؤاد): الاسم الثلاثي كاملاً ورقم الموبايل السوري"
    )
