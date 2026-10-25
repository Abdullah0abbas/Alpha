import logging
from aiogram import BaseMiddleware, Bot
from aiogram.types import Update, Message, CallbackQuery
from aiogram.fsm.context import FSMContext

logger = logging.getLogger(__name__)

class AutoCancelStateOnCommandMiddleware(BaseMiddleware):
    """
    Seamless UX Middleware with Graceful Prompt Fadeout/Auto-Cleanup:
    Whenever a user sends a slash command (e.g., /start, /backup, /admin, /offers, /search, etc.)
    or clicks a primary navigation button while stuck in a pending FSM input state:
    1. It automatically deletes/fades out the old pending prompt message from chat to keep history clean.
    2. It immediately cancels and clears the previous FSM state.
    3. The new command or action executes smoothly with zero clutter or friction.
    """

    # Navigation callbacks that should break any pending text input state
    TOP_LEVEL_NAV_CALLBACKS = {
        "menu_main",
        "menu_user_profile",
        "adm_menu_main",
        "admin_main_menu",
        "admin_refresh_stats",
        "admin_pending_deals",
        "admin_disputes",
        "admin_search_user",
        "admin_broadcast",
        "admin_market_analytics",
        "admin_top_traders",
        "admin_audit_trail",
        "offers_browse",
        "create_offer_start",
    }

    async def __call__(self, handler, event, data: dict):
        state: FSMContext | None = data.get("state")
        bot: Bot | None = data.get("bot")
        
        if state:
            current_state = await state.get_state()
            if current_state is not None:
                msg: Message | None = None
                cb: CallbackQuery | None = None

                if isinstance(event, Message):
                    msg = event
                elif isinstance(event, CallbackQuery):
                    cb = event
                elif isinstance(event, Update):
                    msg = event.message
                    cb = event.callback_query

                should_cancel = False
                chat_id = None

                # 1. If user sent any slash command
                if msg and msg.text and msg.text.startswith("/"):
                    should_cancel = True
                    chat_id = msg.chat.id
                    cmd_name = msg.text.split()[0]
                    logger.info(f"User {msg.from_user.id if msg.from_user else 'unknown'} interrupted state '{current_state}' with command '{cmd_name}'. Auto-clearing & cleaning prompt.")

                # 2. If user clicked a top-level navigation button
                elif cb and cb.data:
                    cb_data = cb.data
                    if (cb_data in self.TOP_LEVEL_NAV_CALLBACKS 
                        or cb_data.startswith("menu_") 
                        or cb_data == "cancel_action" 
                        or cb_data.endswith("_back")
                        or cb_data == "admin_main_menu"):
                        should_cancel = True
                        chat_id = cb.message.chat.id if cb.message else None
                        logger.info(f"User {cb.from_user.id if cb.from_user else 'unknown'} interrupted state '{current_state}' with button '{cb_data}'. Auto-clearing & cleaning prompt.")

                if should_cancel:
                    # Clean up old prompt message if stored in state data
                    try:
                        state_data = await state.get_data()
                        prompt_msg_id = state_data.get("prompt_msg_id") or state_data.get("last_prompt_id")
                        prompt_chat_id = state_data.get("prompt_chat_id") or chat_id

                        if bot and prompt_msg_id and prompt_chat_id:
                            try:
                                await bot.delete_message(chat_id=prompt_chat_id, message_id=prompt_msg_id)
                            except Exception as del_err:
                                # If delete is not permitted or already deleted, try removing markup
                                try:
                                    await bot.edit_message_reply_markup(chat_id=prompt_chat_id, message_id=prompt_msg_id, reply_markup=None)
                                except Exception:
                                    pass
                                logger.debug(f"Note on deleting old prompt: {del_err}")
                    except Exception as e:
                        logger.debug(f"Error during graceful prompt cleanup: {e}")

                    # Fully clear the state
                    await state.clear()

        return await handler(event, data)
