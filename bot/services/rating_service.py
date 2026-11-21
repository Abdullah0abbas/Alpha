from decimal import Decimal, ROUND_HALF_UP
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func

from db.models import DealRating, User


STAR_LABELS = {
    5: "ممتاز",
    4: "جيد جداً",
    3: "مقبول",
    2: "سيئ",
    1: "سيئ جداً",
}


def format_stars(stars: int) -> str:
    """Returns visual star icons matching rating score (1..5)."""
    clamped = max(1, min(5, int(stars)))
    return "⭐️" * clamped


def build_deal_rating_prompt(deal_id: int, counterparty_role_ar: str) -> tuple[str, InlineKeyboardMarkup]:
    """Builds the one-tap post-completion star rating card and inline keyboard."""
    text = (
        f"🌟 **تقييم تجربة التداول — صفقة #{deal_id}**\n"
        f"〰️〰️〰️〰️〰️〰️〰️\n"
        f"تم إتمام الصفقة بنجاح! كيف تقيّم تعاملك مع **{counterparty_role_ar}** في هذه الصفقة؟"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="⭐️⭐️⭐️⭐️⭐️ ممتاز (5)",
                callback_data=f"rate_deal_{deal_id}_5"
            )
        ],
        [
            InlineKeyboardButton(
                text="⭐️⭐️⭐️⭐️ جيد جداً (4)",
                callback_data=f"rate_deal_{deal_id}_4"
            ),
            InlineKeyboardButton(
                text="⭐️⭐️⭐️ مقبول (3)",
                callback_data=f"rate_deal_{deal_id}_3"
            )
        ],
        [
            InlineKeyboardButton(
                text="⭐️⭐️ سيئ (2)",
                callback_data=f"rate_deal_{deal_id}_2"
            ),
            InlineKeyboardButton(
                text="⭐️ سيئ جداً (1)",
                callback_data=f"rate_deal_{deal_id}_1"
            )
        ]
    ])
    return text, kb


async def recalculate_user_reputation(
    db_session: AsyncSession,
    target_user_id: int
) -> tuple[Decimal, int]:
    """
    Recomputes a user's reputation_score (0..100%) from all active DealRating rows.
    Defaults to 100% when the user has 0 ratings (e.g. before first rating or after deletion).
    """
    user_res = await db_session.execute(
        select(User).where(User.telegram_id == target_user_id).with_for_update()
    )
    user = user_res.scalar_one_or_none()

    agg_res = await db_session.execute(
        select(
            func.avg(DealRating.stars),
            func.count(DealRating.id)
        ).where(DealRating.target_id == target_user_id)
    )
    avg_stars, total_count = agg_res.one()
    count_int = int(total_count or 0)

    if count_int == 0 or avg_stars is None:
        new_score = Decimal("100")
    else:
        avg_dec = avg_stars if isinstance(avg_stars, Decimal) else Decimal(str(avg_stars))
        pct = int((avg_dec * Decimal("20")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        pct = max(20, min(100, pct))
        new_score = Decimal(str(pct))

    if user:
        user.reputation_score = new_score

    return new_score, count_int
