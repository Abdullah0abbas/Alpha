from decimal import Decimal
import logging
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, InputMediaPhoto
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from db.models import Deal, User
from db.session import AsyncSessionLocal
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
    "notes": "ADMIN_NOTES_CHANNEL_ID",
    "kyc": "ADMIN_KYC_CHANNEL_ID",
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
    media_group_photos: list[tuple[str, str]] | None = None,
    document_path: str | None = None,
    document_filename: str | None = None,
    exclude_admin_id: int | None = None,
) -> None:
    """
    Centralized dispatcher for the 8 separated Admin Channels with automatic fallback to ADMIN_IDS.
    If sending to the configured channel fails (e.g. bot not added yet or wrong chat ID),
    it automatically falls back to ADMIN_IDS.
    """
    targets = resolve_admin_targets(category, exclude_admin_id=exclude_admin_id)
    attr_name = CHANNEL_CATEGORY_MAP.get(category, "")
    is_dedicated_channel = bool(getattr(settings, attr_name, "").strip()) if attr_name else False

    delivered_to_channel = False

    async def _send_to_target(chat_target: int | str) -> None:
        if media_group_photos:
            media = [
                InputMediaPhoto(media=file_id, caption=cap, parse_mode=parse_mode)
                for file_id, cap in media_group_photos
            ]
            await bot.send_media_group(chat_id=chat_target, media=media)
            await bot.send_message(
                chat_id=chat_target,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
        elif document_path:
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

    for chat_target in targets:
        try:
            await _send_to_target(chat_target)
            if is_dedicated_channel:
                delivered_to_channel = True
        except Exception as e:
            logger.warning(f"Failed to send '{category}' notification to target {chat_target}: {e}")

    # Safety fallback: if a dedicated channel was configured but delivery failed, send to ADMIN_IDS
    if is_dedicated_channel and not delivered_to_channel:
        fallback_admins = [aid for aid in settings.ADMIN_IDS if aid and aid != exclude_admin_id]
        for admin_id in fallback_admins:
            try:
                await _send_to_target(admin_id)
            except Exception as fb_err:
                logger.warning(f"Fallback delivery for '{category}' to admin {admin_id} also failed: {fb_err}")


from datetime import datetime, timezone


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
    deal_id = deal.id
    offer_id = getattr(deal, "offer_id", None)
    seller_id = deal.seller_id
    buyer_id = deal.buyer_id
    amount_usdt = deal.amount_usdt
    amount_syp = deal.amount_syp
    deposit_index = deal.deposit_index
    deposit_address = deal.deposit_address
    seller_fee = getattr(deal, "seller_fee_usdt", None) or Decimal("0")
    buyer_fee = getattr(deal, "buyer_fee_usdt", None) or Decimal("0")
    total_deposited_usdt = amount_usdt + seller_fee

    seller_res = await db_session.execute(select(User).where(User.telegram_id == seller_id))
    seller_user = seller_res.scalar_one_or_none()
    seller_uname = safe_username(seller_user.username if seller_user else None, seller_id)

    buyer_res = await db_session.execute(select(User).where(User.telegram_id == buyer_id))
    buyer_user = buyer_res.scalar_one_or_none()
    buyer_uname = safe_username(buyer_user.username if buyer_user else None, buyer_id)

    rate_val = (amount_syp / amount_usdt) if (amount_usdt and amount_usdt > 0) else Decimal("0")
    tx_line = f"▫️ **هاش المعاملة (TX Hash):** `{escape_markdown(tx_hash)}`\n" if tx_hash else ""
    offer_line = f"▫️ **رقم العرض المرتبط:** `#{offer_id}`\n" if offer_id else ""
    # Never access deal.updated_at after commit because onupdate=func.now() expires the attribute and triggers MissingGreenlet
    dt_str = format_local_datetime(datetime.now(timezone.utc))

    text = (
        f"📥 **إشعار إيداع USDT مؤكد في الضمان — صفقة #{deal_id}**\n"
        f"〰️〰️〰️〰️〰️〰️〰️\n"
        f"{offer_line}"
        f"▫️ **آلية التحقق:** {escape_markdown(verification_method)}\n"
        f"▫️ **التوقيت:** `{dt_str}`\n\n"
        f"💰 **التفاصيل المالية للإيداع:**\n"
        f"▫️ **المبلغ الإجمالي المودع:** `{format_usdt(total_deposited_usdt)} USDT`\n"
        f"▫️ **قيمة الصفقة الأساسية:** `{format_usdt(amount_usdt)} USDT`\n"
        f"▫️ **عمولة البائع المودعة:** `{format_usdt(seller_fee)} USDT`\n"
        f"▫️ **عمولة المشتري (تُخصم عند الإطلاق):** `{format_usdt(buyer_fee)} USDT`\n"
        f"▫️ **المقابل بالليرة السورية:** `{format_syp(amount_syp)}` ل.س (السعر: `{format_syp(rate_val)}` ل.س)\n\n"
        f"🔐 **بيانات محفظة الضمان (BEP-20):**\n"
        f"▫️ **مؤشر الاشتقاق (Index):** `{deposit_index}`\n"
        f"▫️ **عنوان الإيداع:**\n`{escape_markdown(deposit_address)}`\n"
        f"{tx_line}\n"
        f"👥 **أطراف العملية:**\n"
        f"▫️ **🔴 البائع (المودِع):** `{seller_id}` ({seller_uname})\n"
        f"▫️ **🟢 المشتري:** `{buyer_id}` ({buyer_uname})"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔍 فتح ملف الصفقة #{deal_id}", callback_data=f"adm_inspect_deal_{deal_id}")],
        [
            InlineKeyboardButton(text="🔴 ملف البائع", callback_data=f"adm_view_user_{seller_id}"),
            InlineKeyboardButton(text="🟢 ملف المشتري", callback_data=f"adm_view_user_{buyer_id}")
        ]
    ])

    await dispatch_admin_notification(
        bot=bot,
        category="deposits",
        text=text,
        reply_markup=kb,
        parse_mode="Markdown"
    )


async def _freeze_deal_expiry(deal: Deal, db_session: AsyncSession | None = None) -> None:
    """
    Safely freezes deal.expires_at = None in PostgreSQL when a deposit verification failure
    or amount discrepancy occurs, even if the caller did not pass an active db_session.
    """
    if deal.status not in ("CREATED", "AWAITING_USDT_DEPOSIT"):
        return

    deal.expires_at = None
    try:
        if db_session is not None:
            await db_session.execute(
                update(Deal).where(Deal.id == deal.id).values(expires_at=None)
            )
            await db_session.commit()
        else:
            async with AsyncSessionLocal() as fallback_session:
                await fallback_session.execute(
                    update(Deal).where(Deal.id == deal.id).values(expires_at=None)
                )
                await fallback_session.commit()
    except Exception as commit_err:
        logger.warning(f"Failed to freeze expires_at in DB for deal #{deal.id}: {commit_err}")


async def notify_api_verification_failure(
    bot: Bot,
    deal: Deal,
    failure_stage: str,
    error_reason: str,
    db_session: AsyncSession | None = None,
) -> None:
    """
    Sends an actionable alert to Channel #3 (ADMIN_API_FAILURES_CHANNEL_ID)
    whenever the blockchain API / RPC / Watcher fails to verify a deposit or broadcast a payout,
    providing one-click manual confirmation buttons for the admin.
    Automatically freezes deal.expires_at if the deal is in deposit stage so the expiry watcher
    never auto-cancels a deal while it awaits manual admin intervention.
    """
    await _freeze_deal_expiry(deal, db_session)

    seller_fee = getattr(deal, "seller_fee_usdt", None) or Decimal("0")
    buyer_fee = getattr(deal, "buyer_fee_usdt", None) or Decimal("0")
    expected_deposit = deal.amount_usdt + seller_fee
    net_payout = deal.amount_usdt - buyer_fee

    text = (
        f"⚠️ **تنبيه فشل التحقق التلقائي (API / RPC) — صفقة #{deal.id}**\n"
        f"〰️〰️〰️〰️〰️〰️〰️\n"
        f"▫️ **المرحلة المتأثرة:** {escape_markdown(failure_stage)}\n"
        f"▫️ **الحالة الحالية للصفقة:** `{deal.status}` (تم تجميد عداد الإلغاء التلقائي 🛡️)\n"
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
    if deal.status in ("CREATED", "AWAITING_USDT_DEPOSIT", "CANCELLED", "EXPIRED"):
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


async def notify_deposit_amount_mismatch(
    bot: Bot,
    deal: Deal,
    actual_amount: Decimal,
    tx_hash: str | None = None,
    db_session: AsyncSession | None = None,
) -> None:
    """
    Handles underpaid or overpaid USDT escrow deposits strictly:
    - Never auto-confirms if there is any shortage or excess.
    - Freezes deal.expires_at immediately so the deal is never auto-cancelled while funds are in the wallet.
    - Notifies the Seller of the exact received amount and exact remaining/excess balance.
    - Sends an immediate actionable alert to Channel #3 (ADMIN_API_FAILURES_CHANNEL_ID)
      with the exact remaining/excess amount and a manual confirmation button.
    """
    await _freeze_deal_expiry(deal, db_session)

    seller_fee = getattr(deal, "seller_fee_usdt", None) or Decimal("0")
    expected_deposit = deal.amount_usdt + seller_fee
    support_username = (settings.SUPPORT_USERNAME or "AlphaSupport").lstrip("@")
    support_link = f"https://t.me/{support_username}"
    tx_line = f"▫️ **هاش المعاملة (TxHash):** `{escape_markdown(tx_hash)}`\n" if tx_hash else ""

    if actual_amount < expected_deposit:
        remaining = expected_deposit - actual_amount
        # 1. Notify Seller with exact remaining amount and re-check / support buttons
        seller_text = (
            f"⚠️ **رصد إيداع غير مكتمل (نقص في المبلغ) — صفقة #{deal.id}**\n"
            f"〰️〰️〰️〰️〰️〰️〰️\n"
            f"▫️ **المبلغ المطلوب إيداعه:** `{format_usdt(expected_deposit)} USDT`\n"
            f"▫️ **المبلغ الواصل في المحفظة:** `{format_usdt(actual_amount)} USDT`\n"
            f"🔴 **المبلغ المتبقي المطلوب إرساله:** `{format_usdt(remaining)} USDT`\n\n"
            f"📥 **محفظة الإيداع (`BEP-20`):**\n"
            f"`{escape_markdown(deal.deposit_address)}`\n\n"
            f"⚠️ لا يمكن للبوت تأكيد الإيداع آلياً لوجود نقص بمقدار `{format_usdt(remaining)} USDT`.\n"
            f"يرجى تحويل المبلغ المتبقي إلى نفس العنوان أعلاه ثم الضغط على **تأكيد الإيداع** مجدداً، أو التواصل مع الإدارة."
        )
        seller_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 أرسلت المتبقي — إعادة فحص الرصيد", callback_data=f"man_dep_{deal.id}")],
            [InlineKeyboardButton(text="👨‍💻 التواصل مع الدعم الفني", url=support_link)]
        ])
        try:
            await bot.send_message(
                chat_id=deal.seller_id,
                text=seller_text,
                reply_markup=seller_kb,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to send underpayment notice to seller {deal.seller_id} for deal #{deal.id}: {e}")

        # 2. Notify Admin Channel #3 (ADMIN_API_FAILURES_CHANNEL_ID)
        admin_text = (
            f"⚠️ **إيداع USDT ناقص (يتطلب مراجعة أو استكمال) — صفقة #{deal.id}**\n"
            f"〰️〰️〰️〰️〰️〰️〰️\n"
            f"▫️ **المبلغ المطلوب إيداعه:** `{format_usdt(expected_deposit)} USDT`\n"
            f"▫️ **المبلغ الواصل فعلياً:** `{format_usdt(actual_amount)} USDT`\n"
            f"🔴 **المبلغ المتبقي (النقص):** `{format_usdt(remaining)} USDT`\n"
            f"{tx_line}"
            f"▫️ **محفظة الضمان (`#{deal.deposit_index}`):**\n`{escape_markdown(deal.deposit_address)}`\n"
            f"▫️ **البائع:** `{deal.seller_id}` | **المشتري:** `{deal.buyer_id}`\n\n"
            f"ℹ️ تم إشعار البائع بوجود نقص قدره `{format_usdt(remaining)} USDT` لاستكماله، أو يمكنك قبول المبلغ وتأكيد الإيداع يدوياً:"
        )
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ تجاوز النقص وتأكيد الإيداع يدوياً", callback_data=f"adm_ask_manual_dep_{deal.id}")],
            [
                InlineKeyboardButton(text=f"🔍 فحص الصفقة #{deal.id}", callback_data=f"adm_inspect_deal_{deal.id}"),
                InlineKeyboardButton(text="🔴 ملف البائع", callback_data=f"adm_view_user_{deal.seller_id}")
            ]
        ])
        await dispatch_admin_notification(
            bot=bot,
            category="api_failures",
            text=admin_text,
            reply_markup=admin_kb,
            parse_mode="Markdown"
        )

    elif actual_amount > expected_deposit:
        excess = actual_amount - expected_deposit
        # 1. Notify Seller
        seller_text = (
            f"ℹ️ **رصد إيداع بمبلغ أكبر من المطلوب — صفقة #{deal.id}**\n"
            f"〰️〰️〰️〰️〰️〰️〰️\n"
            f"▫️ **المبلغ المطلوب للصفقة:** `{format_usdt(expected_deposit)} USDT`\n"
            f"▫️ **المبلغ الواصل في المحفظة:** `{format_usdt(actual_amount)} USDT`\n"
            f"🟢 **مقدار الزيادة المرصودة:** `+{format_usdt(excess)} USDT`\n\n"
            f"تم تحويل الطلب للإدارة لاعتماد الإيداع يدوياً وتسوية فرق المبلغ. يمكنك أيضاً التواصل مع الدعم الفني:"
        )
        seller_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨‍💻 التواصل مع الدعم الفني", url=support_link)]
        ])
        try:
            await bot.send_message(
                chat_id=deal.seller_id,
                text=seller_text,
                reply_markup=seller_kb,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to send overpayment notice to seller {deal.seller_id} for deal #{deal.id}: {e}")

        # 2. Notify Admin Channel #3 (ADMIN_API_FAILURES_CHANNEL_ID)
        admin_text = (
            f"ℹ️ **إيداع USDT زائد عن المطلوب (يتطلب تأكيداً يدوياً) — صفقة #{deal.id}**\n"
            f"〰️〰️〰️〰️〰️〰️〰️\n"
            f"▫️ **المبلغ المطلوب إيداعه:** `{format_usdt(expected_deposit)} USDT`\n"
            f"▫️ **المبلغ الواصل فعلياً:** `{format_usdt(actual_amount)} USDT`\n"
            f"🟢 **مقدار الزيادة:** `+{format_usdt(excess)} USDT`\n"
            f"{tx_line}"
            f"▫️ **محفظة الضمان (`#{deal.deposit_index}`):**\n`{escape_markdown(deal.deposit_address)}`\n"
            f"▫️ **البائع:** `{deal.seller_id}` | **المشتري:** `{deal.buyer_id}`\n\n"
            f"🛠️ يرجى مراجعة المحفظة وتأكيد الإيداع يدوياً بالأزرار أدناه:"
        )
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ اعتماد الإيداع يدوياً ومتابعة الصفقة", callback_data=f"adm_ask_manual_dep_{deal.id}")],
            [
                InlineKeyboardButton(text=f"🔍 فحص الصفقة #{deal.id}", callback_data=f"adm_inspect_deal_{deal.id}"),
                InlineKeyboardButton(text="🔴 ملف البائع", callback_data=f"adm_view_user_{deal.seller_id}")
            ]
        ])
        await dispatch_admin_notification(
            bot=bot,
            category="api_failures",
            text=admin_text,
            reply_markup=admin_kb,
            parse_mode="Markdown"
        )

