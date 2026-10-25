import re
from typing import Tuple

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
            "عنوان المحفظة غير صالح. يجب أن يبدأ بـ 0x ويتكون من 42 خانة بنظام الست عشري (Hexadecimal)."
        )

    cleaned = "0x" + cleaned[2:]

    if cleaned.lower() in BLACKLISTED_ADDRESSES:
        return False, "", "هذا العنوان محظور (عنوان حرق أو عنوان صفري غير صالح للاستلام)."

    hex_part = cleaned[2:]
    if hex_part.islower() or hex_part.isupper() or hex_part.isdigit():
        normalized = _to_checksum_address(cleaned)
    else:
        if not _is_checksum_address(cleaned):
            return (
                False,
                "",
                "العنوان يحتوي أحرفاً كبيرة وصغيرة غير مطابقة لـ EIP-55 checksum. يرجى التأكد من نسخه بدقة دون تعديل."
            )
        normalized = _to_checksum_address(cleaned)

    if not _is_address(normalized):
        return False, "", "عنوان المحفظة غير صالح."

    return True, normalized, ""
