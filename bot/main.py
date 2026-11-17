import asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from bot.handlers.base import base_router
from bot.handlers.offers import offers_router
from bot.handlers.deals import deals_router
from bot.handlers.negotiations import negotiations_router
from bot.handlers.admin import admin_router
from bot.handlers.profile import profile_router
from bot.middlewares.maintenance import MaintenanceMiddleware
from bot.middlewares.command_cancel import AutoCancelStateOnCommandMiddleware
from bot.middlewares.rate_limit import RateLimitMiddleware
from bot.middlewares.error_alert import handle_critical_error
from bot.tasks import start_expiry_watcher, start_daily_backup_scheduler
from db.session import AsyncSessionLocal, ensure_schema_updated
from config.settings import settings

# Middleware to inject DB session into handlers (Requirement 8.4: Connection pooling via session)
class DbSessionMiddleware:
    async def __call__(self, handler, event, data):
        async with AsyncSessionLocal() as session:
            data['db_session'] = session
            return await handler(event, data)

async def setup_bot_commands(bot: Bot):
    # Public commands for all ordinary users
    public_commands = [
        BotCommand(command="start", description="القائمة الرئيسية"),
        BotCommand(command="continue", description="متابعة صفقتك الحالية"),
        BotCommand(command="support", description="التواصل مع الدعم الفني"),
    ]

    # Secret admin commands for Bot Menu Button (bottom-left)
    admin_commands = [
        BotCommand(command="start", description="القائمة الرئيسية"),
        BotCommand(command="continue", description="متابعة صفقتك الحالية"),
        BotCommand(command="support", description="التواصل مع الدعم الفني"),
        BotCommand(command="admin", description="👑 لوحة الإدارة"),
        BotCommand(command="pending", description="🛡️ الصفقات المعلقة للاعتماد"),
        BotCommand(command="stats", description="📊 الإحصائيات الشاملة"),
        BotCommand(command="market", description="📈 تحليلات وبيانات السوق"),
        BotCommand(command="top_traders", description="🏆 كبار المتداولين"),
        BotCommand(command="disputes", description="⚖️ النزاعات المعلقة"),
        BotCommand(command="search", description="🔍 استعلام عن مستخدم"),
        BotCommand(command="audit", description="📋 سجل التدقيق الجنائي والعمليات"),
        BotCommand(command="broadcast", description="📢 إرسال بث جماعي"),
        BotCommand(command="backup", description="💾 أخذ نسخة احتياطية فورية"),
        BotCommand(command="m_on", description="🟢 تفعيل وضع الصيانة"),
        BotCommand(command="m_off", description="🔴 تعطيل وضع الصيانة"),
    ]

    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
        await bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())
        await bot.set_my_commands(public_commands, scope=BotCommandScopeAllPrivateChats())

        # Register specialized menu for each admin ID specifically
        for admin_id in settings.ADMIN_IDS:
            try:
                await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception as e:
                print(f"Error setting commands for admin {admin_id}: {e}")
    except Exception as e:
        print(f"Error updating bot commands: {e}")
        await bot.set_my_commands(public_commands)

async def main():
    bot_token = settings.BOT_TOKEN
    if not bot_token:
        print("BOT_TOKEN is not set.")
        return

    bot = Bot(token=bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    
    # Register Middleware
    dp.update.middleware(DbSessionMiddleware())
    dp.update.middleware(MaintenanceMiddleware())
    dp.message.middleware(AutoCancelStateOnCommandMiddleware())
    dp.callback_query.middleware(AutoCancelStateOnCommandMiddleware())
    dp.callback_query.middleware(RateLimitMiddleware())

    # Register Global Critical Error Alert Handler
    dp.errors.register(handle_critical_error)
    
    # Register Routers
    dp.include_router(base_router)
    dp.include_router(admin_router)
    dp.include_router(profile_router)
    dp.include_router(offers_router)
    dp.include_router(deals_router)
    dp.include_router(negotiations_router)

    print("Starting Bot in Phase 1 mode (Polling). Webhooks will be enabled in Phase 2/Prod.")
    
    # Auto-patch schema if any migration columns are pending
    await ensure_schema_updated()

    # Setup bot menu commands
    await setup_bot_commands(bot)
    
    # Start periodic deal expiry background worker
    expiry_task = asyncio.create_task(start_expiry_watcher(bot, interval_seconds=30))
    # Start daily automated database backup scheduler (00:00 UTC = 03:00 AM Damascus/Riyadh)
    backup_task = asyncio.create_task(start_daily_backup_scheduler(bot, target_hour_utc=0, target_minute_utc=0))
    
    # For Phase 1, we use polling as explicitly requested by the user, ignoring the webhook spec for now.
    try:
        await dp.start_polling(bot)
    finally:
        expiry_task.cancel()
        backup_task.cancel()
        await bot.session.close()

if __name__ == '__main__':
    asyncio.run(main())
