"""
Unified branding constants and helper functions for Alpha Bot and Alpha P2P.
"""

BOT_NAME = "Alpha Bot"
CHANNEL_NAME = "Alpha P2P"
TAGLINE = "تداول USDT المباشر مقابل الليرة السورية"


def brand_header(title: str | None = None) -> str:
    """Returns a unified RTL header with optional title."""
    if title:
        return f"\u200f**{BOT_NAME}** | {title}"
    return f"\u200f**{BOT_NAME}**"


def brand_footer() -> str:
    """Returns a unified RTL footer."""
    return f"\u200f— {BOT_NAME}"
