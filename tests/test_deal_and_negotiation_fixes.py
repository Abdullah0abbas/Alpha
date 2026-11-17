from datetime import datetime, timezone, timedelta
from decimal import Decimal
import logging
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.test_startup_and_imports import _install_missing_runtime_stubs


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value

    def scalar(self):
        return self._value


class _FakeDeal:
    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", 701)
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeState:
    def __init__(self, initial=None):
        self._data = dict(initial or {})
        self.current_state = None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def set_state(self, st):
        self.current_state = st

    async def clear(self):
        self._data.clear()
        self.current_state = None


class TestDealAndNegotiationFixes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved_modules = sys.modules.copy()
        self._saved_cwd = os.getcwd()
        self._audit_logger = logging.getLogger("audit")
        self._saved_audit_handlers = list(self._audit_logger.handlers)
        self._tmpdir = tempfile.TemporaryDirectory()
        os.chdir(self._tmpdir.name)
        _install_missing_runtime_stubs()

    def tearDown(self):
        os.chdir(self._saved_cwd)
        for h in list(self._audit_logger.handlers):
            if h not in self._saved_audit_handlers:
                self._audit_logger.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
        sys.modules.clear()
        sys.modules.update(self._saved_modules)
        self._tmpdir.cleanup()

    async def test_finalize_deal_creation_uses_actor_id_and_fee_breakdown(self):
        from bot.handlers.deals import creation as creation_mod

        offer = SimpleNamespace(
            id=55,
            user_id=1001,
            offer_type="sell",
            status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            payment_methods=["شام كاش ليرة"],
            payout_details={"شام كاش ليرة": f"شام كاش ليرة: {'a' * 32}"},
            payout_info=None,
        )

        db_session = MagicMock()
        db_session.execute = AsyncMock(return_value=_FakeResult(offer))
        db_session.commit = AsyncMock()
        db_session.refresh = AsyncMock()
        added_objects = []
        db_session.add = lambda obj: added_objects.append(obj)

        # Simulate callback.message whose from_user.id is the Bot's ID (999999), NOT the taker (2002)
        bot_message = MagicMock()
        bot_message.from_user = SimpleNamespace(id=999999)
        bot_message.answer = AsyncMock()
        bot = MagicMock()

        state = _FakeState({"offer_id": 55, "amount_usdt": "100", "amount_syp": "14500"})

        with (
            patch.object(creation_mod, "Deal", _FakeDeal),
            patch.object(creation_mod, "derive_escrow_address", AsyncMock(return_value=("0x_mock_12", 12))),
            patch.object(creation_mod, "record_audit", AsyncMock()) as mock_audit,
            patch.object(creation_mod, "update_channel_offer_message", AsyncMock()),
            patch.object(creation_mod, "push_deal_card", AsyncMock()) as mock_push,
        ):
            await creation_mod._finalize_deal_creation(
                message=bot_message,
                state=state,
                db_session=db_session,
                data={"offer_id": 55, "amount_usdt": "100", "amount_syp": "14500"},
                seller_id=1001,
                buyer_id=2002,
                buyer_payout_address="PENDING",
                seller_payment_info={"method": "شام كاش ليرة", "info": "a" * 32},
                bot=bot,
                actor_id=2002,
            )

        self.assertEqual(len(added_objects), 1)
        created_deal = added_objects[0]
        # > 25 USDT => 0.1% seller + 0.1% buyer = 0.2% total
        self.assertEqual(created_deal.fee_usdt, Decimal("0.2000"))
        self.assertEqual(created_deal.seller_fee_usdt, Decimal("0.1000"))
        self.assertEqual(created_deal.buyer_fee_usdt, Decimal("0.1000"))
        self.assertEqual(offer.status, "reserved")
        mock_audit.assert_awaited_once()
        self.assertEqual(mock_audit.call_args.kwargs["actor_id"], 2002)
        mock_push.assert_awaited_once_with(701, bot)

        # Verify self-deal check triggers when actor_id == offer.user_id even if bot_message.from_user.id != offer.user_id
        offer.status = "active"
        db_session_self = MagicMock()
        db_session_self.execute = AsyncMock(return_value=_FakeResult(offer))
        db_session_self.commit = AsyncMock()
        bot_message.answer.reset_mock()
        with patch.object(creation_mod, "Deal", _FakeDeal):
            await creation_mod._finalize_deal_creation(
                message=bot_message,
                state=_FakeState(),
                db_session=db_session_self,
                data={"offer_id": 55, "amount_usdt": "10", "amount_syp": "1450"},
                seller_id=1001,
                buyer_id=999,
                buyer_payout_address="PENDING",
                seller_payment_info={"method": "شام كاش ليرة", "info": "PENDING"},
                bot=bot,
                actor_id=1001,
            )
        bot_message.answer.assert_awaited_once()
        self.assertIn("قمت بإنشائه بنفسك", bot_message.answer.call_args.args[0])

    async def test_use_saved_seller_info_enforces_method_compatibility(self):
        from bot.handlers.deals import payment_info as pi_mod

        deal = _FakeDeal(
            id=88,
            offer_id=55,
            seller_id=1001,
            buyer_id=2002,
            status="USDT_DEPOSITED",
            seller_payment_info={"method": "شام كاش ليرة", "info": "PENDING"},
        )
        # Seller saved a Syriatel account in profile, but deal requires شام كاش ليرة
        seller_user_bad = SimpleNamespace(telegram_id=1001, saved_payment_info="سيريتيل كاش: 0912345678")

        db_session = MagicMock()
        db_session.execute = AsyncMock(return_value=_FakeResult(seller_user_bad))
        db_session.commit = AsyncMock()

        callback = MagicMock()
        callback.data = "use_saved_seller_info_88"
        callback.from_user = SimpleNamespace(id=1001)
        callback.answer = AsyncMock()
        callback.bot = MagicMock()

        with (
            patch.object(pi_mod, "acquire_deal_lock", AsyncMock(return_value=(True, deal, None, None))),
            patch.object(pi_mod, "apply_seller_payment_info", AsyncMock()) as mock_apply,
        ):
            await pi_mod.cq_use_saved_seller_info(callback, db_session)

        mock_apply.assert_not_awaited()
        callback.answer.assert_awaited_once()
        self.assertIn("لا يطابق", callback.answer.call_args.args[0])
        self.assertTrue(callback.answer.call_args.kwargs.get("show_alert"))

        # Now seller has matching شام كاش ليرة in profile -> accepted cleanly without duplicate prefix
        sham_hex = "c" * 32
        seller_user_ok = SimpleNamespace(telegram_id=1001, saved_payment_info=f"شام كاش ليرة: {sham_hex}")
        db_session.execute = AsyncMock(return_value=_FakeResult(seller_user_ok))
        callback.answer.reset_mock()

        with (
            patch.object(pi_mod, "acquire_deal_lock", AsyncMock(return_value=(True, deal, None, None))),
            patch.object(pi_mod, "push_deal_card", AsyncMock()),
        ):
            await pi_mod.cq_use_saved_seller_info(callback, db_session)

        self.assertEqual(deal.seller_payment_info, {"method": "شام كاش ليرة", "info": sham_hex})
        self.assertEqual(deal.status, "AWAITING_SYP_PAYMENT")
        self.assertIsNotNone(deal.expires_at)
        remaining_secs = (deal.expires_at - datetime.now(timezone.utc)).total_seconds()
        self.assertGreater(remaining_secs, 29 * 60)
        self.assertLessEqual(remaining_secs, 30 * 60 + 5)

        # Buyer submitting payout wallet advances to SYP_PAYMENT_SENT and clears expires_at
        buyer_msg = MagicMock()
        buyer_msg.answer = AsyncMock()
        with (
            patch.object(pi_mod, "push_deal_card", AsyncMock()),
            patch.object(pi_mod, "forward_proof_photo_to_seller", AsyncMock()),
        ):
            await pi_mod.apply_buyer_payout_wallet(
                deal=deal,
                wallet_address="0x" + "a" * 40,
                bot=callback.bot,
                db_session=db_session,
                event=buyer_msg,
                photo_file_id="file_123",
            )
        self.assertEqual(deal.status, "SYP_PAYMENT_SENT")
        self.assertIsNone(deal.expires_at)

    async def test_negotiated_deal_payout_details_and_buyer_pm_choice(self):
        from bot.handlers import negotiations as neg_mod

        sham_hex = "d" * 32
        offer = SimpleNamespace(
            id=77,
            user_id=1001,
            offer_type="sell",
            status="negotiating",
            payment_methods=["شام كاش ليرة", "سيريتيل كاش"],
            payout_details={
                "شام كاش ليرة": f"شام كاش ليرة: {sham_hex}",
                "سيريتيل كاش": "سيريتيل كاش: 0911223344",
            },
            payout_info=f"شام كاش ليرة: {sham_hex}",
        )
        neg = SimpleNamespace(
            id=11,
            offer_id=77,
            maker_id=1001,
            taker_id=2002,
            proposed_amount=Decimal("50"),
            proposed_rate=Decimal("140"),
            status="PENDING_MAKER",
        )

        added_deals = []
        db_session = MagicMock()
        db_session.execute = AsyncMock(return_value=_FakeResult(offer))
        db_session.commit = AsyncMock()
        db_session.refresh = AsyncMock()
        db_session.add = lambda obj: added_deals.append(obj)

        bot = MagicMock()
        bot.send_message = AsyncMock()
        origin_message = MagicMock()
        origin_message.answer = AsyncMock()

        deals_pkg = sys.modules.setdefault("bot.handlers.deals", MagicMock())
        with (
            patch.object(neg_mod, "Deal", _FakeDeal),
            patch.object(deals_pkg, "derive_escrow_address", AsyncMock(return_value=("0x_mock_5", 5))),
            patch.object(neg_mod, "update_channel_offer_message", AsyncMock()),
            patch.object(neg_mod, "push_deal_card", AsyncMock()),
        ):
            ok = await neg_mod._finalize_negotiated_deal(bot, neg, db_session, origin_message)

        self.assertTrue(ok)
        self.assertEqual(len(added_deals), 1)
        new_deal = added_deals[0]
        self.assertEqual(new_deal.seller_payment_info, {"method": "PENDING", "info": "PENDING"})
        # Multi-method Sell offer must prompt the buyer (2002) with nego_pm_{deal_id}_{idx} buttons
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.call_args.kwargs["chat_id"], 2002)
        kb = bot.send_message.call_args.kwargs["reply_markup"]
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertEqual(callbacks, ["nego_pm_701_0", "nego_pm_701_1"])

        # Now buyer taps nego_pm_701_1 (سيريتيل كاش) -> binds clean account cleanly
        db_session_pm = MagicMock()
        db_session_pm.execute = AsyncMock(side_effect=[_FakeResult(new_deal), _FakeResult(offer)])
        db_session_pm.commit = AsyncMock()

        callback_pm = MagicMock()
        callback_pm.data = "nego_pm_701_1"
        callback_pm.from_user = SimpleNamespace(id=2002)
        callback_pm.bot = bot
        callback_pm.message = MagicMock()
        callback_pm.message.edit_text = AsyncMock()
        callback_pm.answer = AsyncMock()

        with patch.object(neg_mod, "push_deal_card", AsyncMock()) as mock_push:
            await neg_mod.cq_nego_choose_payment_method(callback_pm, db_session_pm)

        self.assertEqual(
            new_deal.seller_payment_info,
            {"method": "سيريتيل كاش", "info": "0911223344"},
        )
        mock_push.assert_awaited_once_with(701, bot)

        # Also check Buy offer negotiated deal pulls BEP-20 address from offer.payout_details
        buy_offer = SimpleNamespace(
            id=78,
            user_id=3003,
            offer_type="buy",
            status="negotiating",
            payment_methods=["سيريتيل كاش"],
            payout_details={"usdt_bep20_address": "0x" + "1" * 40},
            payout_info=None,
        )
        buy_neg = SimpleNamespace(
            id=12,
            offer_id=78,
            maker_id=3003,
            taker_id=4004,
            proposed_amount=Decimal("20"),
            proposed_rate=Decimal("140"),
            status="PENDING_MAKER",
        )
        buy_added = []
        db_buy = MagicMock()
        db_buy.execute = AsyncMock(return_value=_FakeResult(buy_offer))
        db_buy.commit = AsyncMock()
        db_buy.refresh = AsyncMock()
        db_buy.add = lambda obj: buy_added.append(obj)
        bot.send_message.reset_mock()

        with (
            patch.object(neg_mod, "Deal", _FakeDeal),
            patch.object(deals_pkg, "derive_escrow_address", AsyncMock(return_value=("0x_mock_6", 6))),
            patch.object(neg_mod, "update_channel_offer_message", AsyncMock()),
            patch.object(neg_mod, "push_deal_card", AsyncMock()),
        ):
            ok_buy = await neg_mod._finalize_negotiated_deal(bot, buy_neg, db_buy, origin_message)

        self.assertTrue(ok_buy)
        self.assertEqual(buy_added[0].buyer_payout_address, "0x" + "1" * 40)
        self.assertEqual(buy_added[0].seller_payment_info, {"method": "سيريتيل كاش", "info": "PENDING"})
        bot.send_message.assert_not_awaited()

    async def test_update_channel_offer_message_without_duration_hours(self):
        from bot.services import channel as channel_mod

        # Offer model does not define duration_hours column; must render active message without AttributeError
        offer = SimpleNamespace(
            id=90,
            offer_type="sell",
            status="active",
            channel_message_id=555,
            payment_methods=["شام كاش ليرة"],
            rate=Decimal("145"),
            min_limit=Decimal("10"),
            max_limit=Decimal("100"),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=3),
        )
        bot = MagicMock()
        bot.edit_message_text = AsyncMock()

        with (
            patch.object(channel_mod.settings, "CHANNEL_ID", -100123456),
            patch.object(channel_mod.settings, "BOT_USERNAME", "AlphaEscrowBot"),
        ):
            await channel_mod.update_channel_offer_message(bot, offer)

        bot.edit_message_text.assert_awaited_once()
        rendered = bot.edit_message_text.call_args.kwargs["text"]
        self.assertIn("عرض #90", rendered)
        self.assertIn("الصلاحية المتبقية", rendered)

    async def test_syp_payment_reminder_and_active_offer_deletion_in_process_expiries(self):
        from bot import tasks as tasks_mod

        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        expired_offer = SimpleNamespace(
            id=301,
            user_id=5005,
            status="active",
            expires_at=past,
            status_changed_at=None,
        )
        syp_deal = SimpleNamespace(
            id=401,
            offer_id=302,
            buyer_id=6006,
            seller_id=7007,
            amount_usdt=Decimal("100"),
            amount_syp=Decimal("14500"),
            status="AWAITING_SYP_PAYMENT",
            expires_at=past,
        )

        class _ScalarIdsResult:
            def __init__(self, ids):
                self._ids = ids

            def scalars(self):
                return SimpleNamespace(all=lambda: list(self._ids))

        class _FakeSessionCtx:
            def __init__(self, session):
                self._session = session

            async def __aenter__(self):
                return self._session

            async def __aexit__(self, exc_type, exc, tb):
                return False

        # process_expiries executes:
        # 1) read deal_ids ([])
        # 2) read active offer_ids ([301]) -> lock offer 301
        # 3) read SYP reminder deal_ids ([401]) -> lock deal 401
        # 4) read neg_ids ([])
        # 5) read orphan offer_ids ([])
        read_empty = MagicMock()
        read_empty.execute = AsyncMock(return_value=_ScalarIdsResult([]))

        read_offers = MagicMock()
        read_offers.execute = AsyncMock(return_value=_ScalarIdsResult([301]))

        lock_offer = MagicMock()
        lock_offer.execute = AsyncMock(return_value=_FakeResult(expired_offer))
        lock_offer.commit = AsyncMock()

        read_syp = MagicMock()
        read_syp.execute = AsyncMock(return_value=_ScalarIdsResult([401]))

        lock_deal = MagicMock()
        lock_deal.execute = AsyncMock(return_value=_FakeResult(syp_deal))
        lock_deal.commit = AsyncMock()

        sessions = iter([
            _FakeSessionCtx(read_empty),
            _FakeSessionCtx(read_offers),
            _FakeSessionCtx(lock_offer),
            _FakeSessionCtx(read_syp),
            _FakeSessionCtx(lock_deal),
            _FakeSessionCtx(read_empty),
            _FakeSessionCtx(read_empty),
        ])

        class _FakeCol:
            def __eq__(self, other): return self
            def __le__(self, other): return self
            def is_not(self, other): return self
            def in_(self, other): return self
            def not_in(self, other): return self

        class _FakeModel:
            id = _FakeCol()
            status = _FakeCol()
            expires_at = _FakeCol()
            offer_id = _FakeCol()

        bot = MagicMock()
        bot.send_message = AsyncMock()

        with (
            patch.object(tasks_mod, "Deal", _FakeModel),
            patch.object(tasks_mod, "Offer", _FakeModel),
            patch.object(tasks_mod, "Negotiation", _FakeModel),
            patch.object(tasks_mod, "select", MagicMock()),
            patch.object(tasks_mod, "AsyncSessionLocal", side_effect=lambda: next(sessions)),
            patch.object(tasks_mod, "update_channel_offer_message", AsyncMock()) as mock_chan_upd,
            patch.object(tasks_mod, "settings", SimpleNamespace(ADMIN_IDS=[9009], SUPPORT_USERNAME="@AlphaSupport")),
        ):
            counts = await tasks_mod.process_expiries(bot)

        self.assertEqual(counts, (0, 1, 1, 0, 0))
        self.assertEqual(expired_offer.status, "deleted")
        mock_chan_upd.assert_awaited_once_with(bot, expired_offer)

        # Deal stays open in AWAITING_SYP_PAYMENT, and expires_at cleared so reminder fires strictly once
        self.assertEqual(syp_deal.status, "AWAITING_SYP_PAYMENT")
        self.assertIsNone(syp_deal.expires_at)

        # Two messages sent total: 1) owner notified of deleted offer 301, 2) buyer 6006 reminded on deal 401 (NO seller/admin!)
        self.assertEqual(bot.send_message.await_count, 2)
        offer_call, syp_call = bot.send_message.await_args_list
        self.assertEqual(offer_call.args[0], 5005)
        self.assertIn("حذف عرضك", offer_call.args[1])

        self.assertEqual(syp_call.kwargs["chat_id"], 6006)
        self.assertIn("@AlphaSupport", syp_call.kwargs["text"])
        syp_kb_callbacks = [btn.callback_data for row in syp_call.kwargs["reply_markup"].inline_keyboard for btn in row]
        self.assertEqual(syp_kb_callbacks, ["man_syp_401"])

    async def test_buyer_wallet_submission_and_saved_gate_during_syp_payment_sent(self):
        from bot.handlers.deals import payment_info as pi_mod
        from bot.handlers.deals import settlement as set_mod

        valid_wallet = "0x55d398326f99059ff775485246999027b3197955"
        expected_checksum = "0x55d398326f99059fF775485246999027B3197955"

        # Case 1: Buyer without saved wallet submits photo -> SYP_PAYMENT_SENT -> submits wallet manually
        deal_no_saved = _FakeDeal(
            id=801,
            seller_id=1001,
            buyer_id=2002,
            amount_usdt=Decimal("50"),
            amount_syp=Decimal("7250"),
            buyer_payout_address="PENDING",
            seller_payment_info={"method": "شام كاش ليرة", "info": "a" * 32},
            status="AWAITING_SYP_PAYMENT",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=25),
        )
        buyer_user_none = SimpleNamespace(telegram_id=2002, saved_usdt_address=None)

        db_photo = MagicMock()
        db_photo.execute = AsyncMock(side_effect=[_FakeResult(None), _FakeResult(buyer_user_none)])
        db_photo.add = MagicMock()
        db_photo.commit = AsyncMock()

        photo_msg = MagicMock()
        photo_msg.from_user = SimpleNamespace(id=2002)
        photo_msg.photo = [SimpleNamespace(file_id="pic_abc")]
        photo_msg.answer = AsyncMock()
        photo_msg.bot = MagicMock()
        state = _FakeState({"deal_id": 801})

        with (
            patch.object(set_mod, "acquire_deal_lock", AsyncMock(return_value=(True, deal_no_saved, None, None))),
            patch.object(set_mod, "push_deal_card", AsyncMock()),
            patch.object(set_mod, "forward_proof_photo_to_seller", AsyncMock()) as mock_fwd,
        ):
            await set_mod.process_syp_proof_photo(photo_msg, state, db_photo)

        self.assertEqual(deal_no_saved.status, "SYP_PAYMENT_SENT")
        self.assertIsNone(deal_no_saved.expires_at)
        mock_fwd.assert_awaited_once()
        self.assertEqual(state._data.get("deal_id"), 801)
        self.assertIn("BEP-20", photo_msg.answer.call_args.args[0])

        # Now buyer replies with BEP-20 address while deal is SYP_PAYMENT_SENT
        db_wallet = MagicMock()
        db_wallet.commit = AsyncMock()
        wallet_msg = MagicMock()
        wallet_msg.from_user = SimpleNamespace(id=2002)
        wallet_msg.text = valid_wallet
        wallet_msg.answer = AsyncMock()
        wallet_msg.bot = MagicMock()

        with (
            patch.object(pi_mod, "acquire_deal_lock", AsyncMock(return_value=(True, deal_no_saved, None, None))),
            patch.object(pi_mod, "push_deal_card", AsyncMock()) as mock_push_buyer,
        ):
            await pi_mod.process_set_buyer_wallet(wallet_msg, state, db_wallet)

        self.assertEqual(deal_no_saved.buyer_payout_address, expected_checksum)
        self.assertEqual(deal_no_saved.status, "SYP_PAYMENT_SENT")
        wallet_msg.answer.assert_awaited_once()
        self.assertIn("تم تسجيل عنوان محفظة الاستلام", wallet_msg.answer.call_args.args[0])
        mock_push_buyer.assert_awaited_once_with(801, wallet_msg.bot, notify_handoff=False, specific_user_id=2002)

        # Case 2: Buyer WITH saved wallet gets confirmation gate after photo, not silent overwrite
        deal_saved = _FakeDeal(
            id=802,
            seller_id=1001,
            buyer_id=2002,
            amount_usdt=Decimal("50"),
            amount_syp=Decimal("7250"),
            buyer_payout_address="PENDING",
            seller_payment_info={"method": "شام كاش ليرة", "info": "a" * 32},
            status="AWAITING_SYP_PAYMENT",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=25),
        )
        buyer_user_saved = SimpleNamespace(telegram_id=2002, saved_usdt_address=valid_wallet)
        db_gate = MagicMock()
        db_gate.execute = AsyncMock(side_effect=[_FakeResult(None), _FakeResult(buyer_user_saved)])
        db_gate.add = MagicMock()
        db_gate.commit = AsyncMock()
        photo_msg.answer.reset_mock()

        with (
            patch.object(set_mod, "acquire_deal_lock", AsyncMock(return_value=(True, deal_saved, None, None))),
            patch.object(set_mod, "push_deal_card", AsyncMock()),
            patch.object(set_mod, "forward_proof_photo_to_seller", AsyncMock()),
        ):
            await set_mod.process_syp_proof_photo(photo_msg, _FakeState({"deal_id": 802}), db_gate)

        self.assertEqual(deal_saved.status, "SYP_PAYMENT_SENT")
        self.assertEqual(deal_saved.buyer_payout_address, "PENDING")
        gate_kb = photo_msg.answer.call_args.kwargs["reply_markup"]
        gate_cbs = [btn.callback_data for row in gate_kb.inline_keyboard for btn in row]
        self.assertIn("use_saved_buyer_wallet_802", gate_cbs)
        self.assertIn("manual_set_buyer_wallet_802", gate_cbs)

    async def test_offer_wizard_saved_payment_info_reuse_and_man_dep_auto_advance(self):
        from bot.handlers import offers as off_mod
        from bot.handlers.deals import settlement as set_mod

        sham_hex = "e" * 32
        user_sham = SimpleNamespace(telegram_id=111, saved_payment_info=f"شام كاش ليرة: {sham_hex}")
        self.assertEqual(off_mod._extract_saved_payment_info(user_sham, "شام كاش ليرة"), sham_hex)
        self.assertIsNone(off_mod._extract_saved_payment_info(user_sham, "سيريتيل كاش"))

        user_syriatel = SimpleNamespace(telegram_id=112, saved_payment_info="سيريتيل كاش: 0912345678")
        self.assertEqual(off_mod._extract_saved_payment_info(user_syriatel, "سيريتيل كاش"), "0912345678")

        user_haram = SimpleNamespace(telegram_id=113, saved_payment_info="حوالة بنكية (الهرم): محمد أحمد علي - 0912345678")
        self.assertIsNone(off_mod._extract_saved_payment_info(user_haram, "حوالة بنكية (الفؤاد)"))
        self.assertIsNotNone(off_mod._extract_saved_payment_info(user_haram, "حوالة بنكية (الهرم)"))

        # Simulate clicking wiz_use_saved_pm_0 with Sham Cash Lira
        state = _FakeState({
            "offer_type": "sell",
            "payment_methods": ["شام كاش ليرة"],
            "pending_payout_methods": ["شام كاش ليرة"],
            "current_payout_index": 0,
            "payout_details": {},
        })
        cb_saved = MagicMock()
        cb_saved.from_user = SimpleNamespace(id=111)
        cb_saved.answer = AsyncMock()
        cb_saved.message = MagicMock()
        cb_saved.message.edit_text = AsyncMock()
        db_saved = MagicMock()
        db_saved.execute = AsyncMock(return_value=_FakeResult(user_sham))

        await off_mod.use_saved_payment_method_info(cb_saved, state, db_saved)
        self.assertEqual(state._data["payout_details"]["شام كاش ليرة"], f"شام كاش ليرة: {sham_hex}")
        cb_saved.answer.assert_awaited_with("تم اعتماد الحساب.")

        # man_dep_ with populated seller_payment_info must auto-advance without contradictory buyer notice
        deal_ready = _FakeDeal(
            id=905,
            seller_id=111,
            buyer_id=222,
            amount_usdt=Decimal("40"),
            amount_syp=Decimal("5800"),
            seller_payment_info={"method": "شام كاش ليرة", "info": sham_hex},
            status="AWAITING_USDT_DEPOSIT",
            expires_at=None,
        )
        cb_dep = MagicMock()
        cb_dep.data = "man_dep_905"
        cb_dep.from_user = SimpleNamespace(id=111)
        cb_dep.answer = AsyncMock()
        cb_dep.bot = MagicMock()
        cb_dep.bot.send_message = AsyncMock()
        db_dep = MagicMock()
        db_dep.commit = AsyncMock()

        with (
            patch.object(set_mod, "acquire_deal_lock", AsyncMock(return_value=(True, deal_ready, None, None))),
            patch.object(set_mod, "push_deal_card", AsyncMock()) as mock_push,
        ):
            await set_mod.manual_usdt_deposited(cb_dep, _FakeState(), db_dep)

        self.assertEqual(deal_ready.status, "AWAITING_SYP_PAYMENT")
        self.assertIsNotNone(deal_ready.expires_at)
        cb_dep.bot.send_message.assert_not_awaited()
        mock_push.assert_awaited_once_with(905, cb_dep.bot)

    async def test_seller_proof_photo_preserved_on_confirm_and_dispute(self):
        from bot.handlers.deals import settlement as set_mod

        # 1. Confirming SYP receipt on a photo message edits caption, clears seller_card_msg_id, and never calls edit_text
        deal_conf = _FakeDeal(
            id=910,
            seller_id=1001,
            buyer_id=2002,
            amount_usdt=Decimal("100"),
            amount_syp=Decimal("1450000"),
            buyer_payout_address="0x" + "b" * 40,
            seller_payment_info={"method": "سيريتيل كاش", "info": "0912345678"},
            seller_card_msg_id=777,
            status="SYP_PAYMENT_SENT",
        )
        cb_conf = MagicMock()
        cb_conf.data = "man_conf_910"
        cb_conf.from_user = SimpleNamespace(id=1001)
        cb_conf.answer = AsyncMock()
        cb_conf.bot = MagicMock()
        cb_conf.bot.send_message = AsyncMock()
        cb_conf.message = MagicMock()
        cb_conf.message.photo = [SimpleNamespace(file_id="proof_photo_1")]
        cb_conf.message.edit_caption = AsyncMock()
        cb_conf.message.edit_text = AsyncMock()

        db_conf = MagicMock()
        db_conf.commit = AsyncMock()

        with (
            patch.object(set_mod, "acquire_deal_lock", AsyncMock(return_value=(True, deal_conf, None, None))),
            patch.object(set_mod, "format_admin_deal_review_text", AsyncMock(return_value="ADMIN_REVIEW")),
            patch.object(set_mod, "push_deal_card", AsyncMock()) as mock_push_conf,
        ):
            await set_mod.manual_syp_confirmed(cb_conf, db_conf)

        self.assertEqual(deal_conf.status, "ADMIN_APPROVAL")
        self.assertIsNone(deal_conf.seller_card_msg_id)
        cb_conf.message.edit_caption.assert_awaited_once()
        cb_conf.message.edit_text.assert_not_awaited()
        self.assertIsNone(cb_conf.message.edit_caption.call_args.kwargs.get("reply_markup"))
        mock_push_conf.assert_awaited_once_with(910, cb_conf.bot, notify_handoff=False)

        # 2. Disputing non-receipt on a photo message edits caption, clears seller_card_msg_id, and never calls edit_text
        deal_disp = _FakeDeal(
            id=911,
            seller_id=1001,
            buyer_id=2002,
            amount_usdt=Decimal("50"),
            amount_syp=Decimal("725000"),
            seller_card_msg_id=778,
            status="SYP_PAYMENT_SENT",
        )
        cb_disp = MagicMock()
        cb_disp.data = "dispute_deal_911"
        cb_disp.from_user = SimpleNamespace(id=1001)
        cb_disp.answer = AsyncMock()
        cb_disp.bot = MagicMock()
        cb_disp.bot.send_message = AsyncMock()
        cb_disp.message = MagicMock()
        cb_disp.message.photo = [SimpleNamespace(file_id="proof_photo_2")]
        cb_disp.message.edit_caption = AsyncMock()
        cb_disp.message.edit_text = AsyncMock()

        db_disp = MagicMock()
        db_disp.commit = AsyncMock()

        with (
            patch.object(set_mod, "acquire_deal_lock", AsyncMock(return_value=(True, deal_disp, None, None))),
            patch.object(set_mod, "add_audit", MagicMock()),
            patch.object(set_mod, "push_deal_card", AsyncMock()) as mock_push_disp,
        ):
            await set_mod.handle_non_receipt_dispute(cb_disp, db_disp)

        self.assertEqual(deal_disp.status, "DISPUTED")
        self.assertIsNone(deal_disp.seller_card_msg_id)
        cb_disp.message.edit_caption.assert_awaited_once()
        cb_disp.message.edit_text.assert_not_awaited()
        mock_push_disp.assert_awaited_once_with(911, cb_disp.bot, notify_handoff=False)


if __name__ == "__main__":
    unittest.main()
