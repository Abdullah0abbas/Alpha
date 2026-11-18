from decimal import Decimal
import logging
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from db.models import Deal, User
from config.settings import settings
from bot.utils.formatters import format_usdt, format_syp, safe_username, escape_markdown, format_local_datetime

logger = logging.getLogger(__name__)


CHANNEL_CATEGORY_MAP = {
    "deposits": "ADMIN_DEPOSITS_CHANNEL_ID",
    "payouts": "ADMIN_PAYOUTS_CHANNEL_ID",
    "api_failures": "ADMIN_API_FAILURES_CHANNEL_ID",
    "disputes": "ADMIN_DISPUTES_CHANNEL_ID",
    "errors": "ADMIN_ERRORS_CHANNEL_ID",
    "backups": "ADMIN_BACKUPS_CHANNEL_ID",
}


def resolve_admin_targets(category: str, exclude_admin_id: int | None = None) -> list[int | str]:
    """
    Resolves the target chat ID(s) for a specific admin notification category.
    If the dedicated channel/group ID is configured in settings, returns [channel_id].
    Otherwise falls back to settings.ADMIN_IDS so no financial or technical alert is ever lost.
    """
    attr_name = CHANNEL_CATEGORY_MAP.get(category, "")
    raw_channel = getattr(settings, attr_name, "") if attr_name else ""
    channel_val = str(raw_channel).strip() if raw_channel else ""

    if channel_val:
        if channel_val.lstrip("-").isdigit():
            return [int(channel_val)]
        return [channel_val]

    return [aid for aid in settings.ADMIN_IDS if aid and aid != exclude_admin_id]


async def dispatch_admin_notification(
    bot: Bot,
    category: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "Markdown",
    photo_file_id: str | None = None,
    document_path: str | None = None,
    document_filename: str | None = None,
    exclude_admin_id: int | None = None,
) -> None:
    """
    Centralized dispatcher for the 6 separated Admin Channels with automatic fallback to ADMIN_IDS.
    If sending to the configured channel fails (e.g. bot not added yet or wrong chat ID),
    it automatically falls back to ADMIN_IDS.
    """
    targets = resolve_admin_targets(category, exclude_admin_id=exclude_admin_id)
    attr_name = CHANNEL_CATEGORY_MAP.get(category, "")
    is_dedicated_channel = bool(getattr(settings, attr_name, "").strip()) if attr_name else False

    delivered_to_channel = False

    for chat_target in targets:
        try:
            if document_path:
                doc = FSInputFile(path=document_path, filename=document_filename)
                await bot.send_document(
                    chat_id=chat_target,
                    document=doc,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode
                )
            elif photo_file_id:
                await bot.send_photo(
                    chat_id=chat_target,
                    photo=photo_file_id,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode
                )
            else:
                await bot.send_message(
                    chat_id=chat_target,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode
                )
            if is_dedicated_channel:
                delivered_to_channel = True
        except Exception as e:
            logger.warning(f"Failed to send '{category}' notification to target {chat_target}: {e}")

    # Safety fallback: if a dedicated channel was configured but delivery failed, send to ADMIN_IDS
    if is_dedicated_channel and not delivered_to_channel:
        fallback_admins = [aid for aid in settings.ADMIN_IDS if aid and aid != exclude_admin_id]
        for admin_id in fallback_admins:
            try:
                if document_path:
                    doc = FSInputFile(path=document_path, filename=document_filename)
                    await bot.send_document(
                        chat_id=admin_id,
                        document=doc,
                        caption=text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode
                    )
                elif photo_file_id:
                    await bot.send_photo(
                        chat_id=admin_id,
                        photo=photo_file_id,
                        caption=text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode
                    )
                else:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode
                    )
            except Exception as fb_err:
                logger.warning(f"Fallback delivery for '{category}' to admin {admin_id} also failed: {fb_err}")


async def notify_usdt_deposit_confirmed(
    bot: Bot,
    deal: Deal,
    db_session: AsyncSession,
    verification_method: str = "تأكيد إيداع الضمان",
    tx_hash: str | None = None,
) -> None:
    """
    Sends a rich, complete notification to Channel #1 (ADMIN_DEPOSITS_CHANNEL_ID)
    the moment a user deposits USDT into escrow and the bot verifies it.
    """
    seller_fee = getattr(deal, "seller_fee_usdt", None) or Decimal("0")
    buyer_fee = getattr(deal, "buyer_fee_usdt", None) or Decimal("0")
    total_deposited_usdt = deal.amount_usdt + seller_fee

    seller_res = await db_session.execute(select(User).where(User.telegram_id == deal.seller_id))
    seller_user = seller_res.scalar_one_or_none()
    seller_uname = safe_username(seller_user.username if seller_user else None, deal.seller_id)

    buyer_res = await db_session.execute(select(User).where(User.telegram_id == deal.buyer_id))
    buyer_user = buyer_res.scalar_one_or_none()
    buyer_uname = safe_username(buyer_user.username if buyer_user else None, deal.buyer_id)

    rate_val = (deal.amount_syp / deal.amount_usdt) if (deal.amount_usdt and deal.amount_usdt > 0) else Decimal("0")
    tx_line = f"▫️ **هاش المعاملة (TX Hash):** `{escape_markdown(tx_hash)}`\n" if tx_hash else ""
    offer_line = f"▫️ **رقم العرض المرتبط:** `#{deal.offer_id}`\n" if getattr(deal, "offer_id", None) else ""
    dt_str = format_local_datetime(deal.updated_at or deal.created_at)

    text = (
        f"📥 **إشعار إيداع USDT مؤكد في الضمان — صفقة #{deal.id}**\n"
        f"〰️〰️〰️〰️〰️〰️〰️\n"
        f"{offer_line}"
        f"▫️ **آلية التحقق:** {escape_markdown(verification_method)}\n"
        f"▫️ **التوقيت:** `{dt_str}`\n\n"
        f"💰 **التفاصيل المالية للإيداع:**\n"
        f"▫️ **المبلغ الإجمالي المودع:** `{format_usdt(total_deposited_usdt)} USDT`\n"
        f"▫️ **قيمة الصفقة الأساسية:** `{format_usdt(deal.amount_usdt)} USDT`\n"
        f"▫️ **عمولة البائع المودعة:** `{format_usdt(seller_fee)} USDT`\n"
        f"▫️ **عمولة المشتري (تُخصم عند الإطلاق):** `{format_usdt(buyer_fee)} USDT`\n"
        f"▫️ **المقابل بالليرة السورية:** `{format_syp(deal.amount_syp)}` ل.س (السعر: `{format_syp(rate_val)}` ل.س)\n\n"
        f"🔐 **بيانات محفظة الضمان (BEP-20):**\n"
        f"▫️ **مؤشر الاشتقاق (Index):** `{deal.deposit_index}`\n"
        f"▫️ **عنوان الإيداع:**\n`{escape_markdown(deal.deposit_address)}`\n"
        f"{tx_line}\n"
        f"👥 **أطراف العملية:**\n"
        f"▫️ **🔴 البائع (المودِع):** `{deal.seller_id}` ({seller_uname})\n"
        f"▫️ **🟢 المشتري:** `{deal.buyer_id}` ({buyer_uname})"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔍 فتح ملف الصفقة #{deal.id}", callback_data=f"adm_inspect_deal_{deal.id}")],
        [
            InlineKeyboardButton(text="🔴 ملف البائع", callback_data=f"adm_view_user_{deal.seller_id}"),
            InlineKeyboardButton(text="🟢 ملف المشتري", callback_data=f"adm_view_user_{deal.buyer_id}")
        ]
    ])

    await dispatch_admin_notification(
        bot=bot,
        category="deposits",
        text=text,
        reply_markup=kb,
        parse_mode="Markdown"
    )


async def notify_api_verification_failure(
    bot: Bot,
    deal: Deal,
    failure_stage: str,
    error_reason: str,
) -> None:
    """
    Sends an actionable alert to Channel #3 (ADMIN_API_FAILURES_CHANNEL_ID)
    whenever the blockchain API / RPC / Watcher fails to verify a deposit or broadcast a payout,
    providing one-click manual confirmation buttons for the admin.
    """
    seller_fee = getattr(deal, "seller_fee_usdt", None) or Decimal("0")
    buyer_fee = getattr(deal, "buyer_fee_usdt", None) or Decimal("0")
    expected_deposit = deal.amount_usdt + seller_fee
    net_payout = deal.amount_usdt - buyer_fee

    text = (
        f"⚠️ **تنبيه فشل التحقق التلقائي (API / RPC) — صفقة #{deal.id}**\n"
        f"〰️〰️〰️〰️〰️〰️〰️\n"
        f"▫️ **المرحلة المتأثرة:** {escape_markdown(failure_stage)}\n"
        f"▫️ **الحالة الحالية للصفقة:** `{deal.status}`\n"
        f"▫️ **سبب الفشل التقني:** `{escape_markdown(error_reason[:200])}`\n\n"
        f"💰 **بيانات المطابقة اليدوية:**\n"
        f"▫️ **المبلغ المتوقع إيداعه:** `{format_usdt(expected_deposit)} USDT`\n"
        f"▫️ **صافي الإرسال للمشتري:** `{format_usdt(net_payout)} USDT`\n"
        f"▫️ **محفظة إيداع الصفقة (`#{deal.deposit_index}`):**\n`{escape_markdown(deal.deposit_address)}`\n"
        f"▫️ **محفظة استلام المشتري:**\n`{escape_markdown(deal.buyer_payout_address or 'غير محددة')}`\n"
        f"▫️ **البائع:** `{deal.seller_id}` | **المشتري:** `{deal.buyer_id}`\n\n"
        f"🛠️ **يمكنك التحقق من الشبكة وتأكيد العملية يدوياً بالأزرار أدناه:**"
    )

    rows = []
    if deal.status in ("CREATED", "AWAITING_USDT_DEPOSIT"):
        rows.append([
            InlineKeyboardButton(
                text="✅ تأكيد استلام إيداع الـ USDT يدوياً",
                callback_data=f"adm_ask_manual_dep_{deal.id}"
            )
        ])
    if deal.status == "ADMIN_APPROVAL":
        rows.append([
            InlineKeyboardButton(
                text="☑️ اعتماد وإطلاق الضمان يدوياً",
                callback_data=f"ask_rel_{deal.id}"
            )
        ])
    rows.append([
        InlineKeyboardButton(text=f"🔍 فحص الصفقة #{deal.id}", callback_data=f"adm_inspect_deal_{deal.id}")
    ])

    await dispatch_admin_notification(
        bot=bot,
        category="api_failures",
        text=text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="Markdown"
    )
