import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
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

logger = logging.getLogger(__name__)


# Middleware to inject DB session into handlers with automatic rollback on unhandled error
class DbSessionMiddleware:
    async def __call__(self, handler, event, data):
        async with AsyncSessionLocal() as session:
            data['db_session'] = session
            try:
                return await handler(event, data)
            except Exception:
                try:
                    await session.rollback()
                except Exception:
                    pass
                raise


async def setup_bot_commands(bot: Bot):
    # Public commands for all ordinary users
    public_commands = [
        BotCommand(command="start", description="القائمة الرئيسية"),
        BotCommand(command="continue", description="متابعة صفقتك الحالية"),
        BotCommand(command="support", description="التواصل مع الدعم الفني"),
    ]

    # Streamlined admin commands for Bot Menu Button (bottom-left)
    admin_commands = [
        BotCommand(command="start", description="القائمة الرئيسية"),
        BotCommand(command="continue", description="متابعة صفقتك الحالية"),
        BotCommand(command="support", description="التواصل مع الدعم الفني"),
        BotCommand(command="admin", description="👑 لوحة التحكم وغرفة العمليات"),
        BotCommand(command="deal", description="🔍 استعلام عن عملية ربط / صفقة"),
        BotCommand(command="user", description="👤 استعلام عن مستخدم وإدارة حسابه"),
        BotCommand(command="pending", description="📡 العمليات الجارية والصفقات المعلقة"),
        BotCommand(command="backup", description="💾 نسخة احتياطية فورية لقاعدة البيانات"),
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
                logger.warning(f"Error setting commands for admin {admin_id}: {e}")
    except Exception as e:
        logger.warning(f"Error updating bot commands: {e}")
        try:
            await bot.set_my_commands(public_commands)
        except Exception:
            pass


async def supervise_background_workers(bot: Bot):
    """
    Watchdog supervisor that guarantees both background tasks (expiry watcher & daily backup)
    remain alive continuously and automatically respawns either task if it ever exits unexpectedly.
    """
    expiry_task: asyncio.Task | None = None
    backup_task: asyncio.Task | None = None

    try:
        while True:
            if expiry_task is None or expiry_task.done():
                if expiry_task is not None and not expiry_task.cancelled():
                    exc = expiry_task.exception()
                    logger.critical(f"Watchdog: expiry_task terminated unexpectedly ({exc}), respawning immediately...")
                expiry_task = asyncio.create_task(start_expiry_watcher(bot, interval_seconds=30))

            if backup_task is None or backup_task.done():
                if backup_task is not None and not backup_task.cancelled():
                    exc = backup_task.exception()
                    logger.critical(f"Watchdog: backup_task terminated unexpectedly ({exc}), respawning immediately...")
                backup_task = asyncio.create_task(start_daily_backup_scheduler(bot, target_hour_utc=0, target_minute_utc=0))

            await asyncio.sleep(30)
    except asyncio.CancelledError:
        for task in (expiry_task, backup_task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(
            *(t for t in (expiry_task, backup_task) if t is not None),
            return_exceptions=True
        )
        raise


def build_dispatcher() -> Dispatcher:
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

    return dp


async def main():
    bot_token = settings.BOT_TOKEN
    if not bot_token:
        logger.critical("BOT_TOKEN is not set.")
        return

    dp = build_dispatcher()
    backoff_seconds = 5

    # Self-healing outer supervisor loop: guarantees the bot never dies or stays frozen
    while True:
        session = AiohttpSession(timeout=30)
        bot = Bot(token=bot_token, session=session)
        watchdog_task: asyncio.Task | None = None

        try:
            logger.info("Starting Bot in resilient Polling mode...")

            # 1. Auto-patch schema with timeout guard
            try:
                await asyncio.wait_for(ensure_schema_updated(), timeout=45)
            except Exception as schema_err:
                logger.error(f"Startup schema check warning: {schema_err}", exc_info=True)

            # 2. Setup bot menu commands with timeout guard so Telegram lag never blocks startup
            try:
                await asyncio.wait_for(setup_bot_commands(bot), timeout=20)
            except Exception as cmd_err:
                logger.warning(f"Startup setup_bot_commands timed out or failed: {cmd_err}")

            # 3. Start supervised background workers (expiry watcher + daily backup)
            watchdog_task = asyncio.create_task(supervise_background_workers(bot))

            # Reset backoff once startup succeeds
            backoff_seconds = 5

            # 4. Run polling with bounded polling_timeout
            await dp.start_polling(
                bot,
                polling_timeout=20,
                handle_signals=False,
            )
            logger.warning("dp.start_polling exited normally; restarting polling loop in 5s...")

        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            logger.info("Bot shutdown signal received; stopping cleanly.")
            break
        except Exception as crash_err:
            logger.critical(
                f"Self-Healing Supervisor: Bot polling loop crashed ({crash_err}). "
                f"Restarting automatically in {backoff_seconds}s...",
                exc_info=True
            )
        finally:
            if watchdog_task and not watchdog_task.done():
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except Exception:
                    pass
            try:
                await bot.session.close()
            except Exception:
                pass

        await asyncio.sleep(backoff_seconds)
        backoff_seconds = min(backoff_seconds * 2, 60)


if __name__ == '__main__':
    asyncio.run(main())

