import unittest
from datetime import datetime, timezone, timedelta

from bot.utils.transitions import (
    can_expire_deal,
    can_enter_grace_period,
    can_permanently_expire_offer,
    can_expire_negotiation,
    can_extend_offer,
    can_delete_offer,
    can_maker_reject_negotiation,
    can_taker_reject_negotiation,
    can_maker_counter_negotiation,
    resolve_deal_parties,
    can_cancel_deal,
    can_seller_confirm_syp,
    can_admin_release_escrow,
    get_next_deal_status_after_syp_confirmed,
    SELLER_INFO_ALLOWED_STATUSES,
    should_release_orphan_offer,
    can_user_interact_with_offer,
)


class TestTransitions(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
        self.past = self.now - timedelta(minutes=5)
        self.future = self.now + timedelta(minutes=15)

    def test_can_expire_deal_valid_cases(self):
        for status in ("PENDING_OWNER_APPROVAL", "AWAITING_USDT_DEPOSIT", "CREATED"):
            self.assertTrue(can_expire_deal(status, self.past, self.now))

    def test_can_expire_deal_race_and_invalid_cases(self):
        # Time not expired yet
        self.assertFalse(can_expire_deal("PENDING_OWNER_APPROVAL", self.future, self.now))
        # Status changed before lock (e.g., already USDT_DEPOSITED, COMPLETED, or CANCELLED)
        self.assertFalse(can_expire_deal("USDT_DEPOSITED", self.past, self.now))
        self.assertFalse(can_expire_deal("COMPLETED", self.past, self.now))
        self.assertFalse(can_expire_deal("EXPIRED", self.past, self.now))
        # Missing expires_at
        self.assertFalse(can_expire_deal("PENDING_OWNER_APPROVAL", None, self.now))

    def test_can_enter_grace_period(self):
        # Active offer expired -> allowed
        self.assertTrue(can_enter_grace_period("active", self.past, self.now))
        # Time not expired
        self.assertFalse(can_enter_grace_period("active", self.future, self.now))
        # Race condition: Offer was reserved for a deal before lock -> strictly forbidden
        self.assertFalse(can_enter_grace_period("reserved", self.past, self.now))
        # Race condition: Offer is in negotiation -> strictly forbidden
        self.assertFalse(can_enter_grace_period("negotiating", self.past, self.now))
        # Already cancelled, expired, or completed
        self.assertFalse(can_enter_grace_period("cancelled", self.past, self.now))
        self.assertFalse(can_enter_grace_period("expired", self.past, self.now))

    def test_can_permanently_expire_offer(self):
        self.assertTrue(can_permanently_expire_offer("expired_grace", self.past, self.now))
        self.assertFalse(can_permanently_expire_offer("expired_grace", self.future, self.now))
        # Not in expired_grace (e.g. was extended back to active)
        self.assertFalse(can_permanently_expire_offer("active", self.past, self.now))

    def test_can_expire_negotiation(self):
        self.assertTrue(can_expire_negotiation("PENDING_MAKER", self.past, self.now))
        self.assertTrue(can_expire_negotiation("PENDING_TAKER", self.past, self.now))
        # Time not expired
        self.assertFalse(can_expire_negotiation("PENDING_MAKER", self.future, self.now))
        # Race condition: Negotiation already ACCEPTED before lock
        self.assertFalse(can_expire_negotiation("ACCEPTED", self.past, self.now))
        # Negotiation already REJECTED or EXPIRED
        self.assertFalse(can_expire_negotiation("REJECTED", self.past, self.now))
        self.assertFalse(can_expire_negotiation("EXPIRED", self.past, self.now))

    def test_can_extend_offer(self):
        # Allowed statuses
        can_ext_active, _ = can_extend_offer("active")
        self.assertTrue(can_ext_active)
        can_ext_grace, _ = can_extend_offer("expired_grace")
        self.assertTrue(can_ext_grace)

        # Race conditions & disallowed statuses
        can_ext_res, reason_res = can_extend_offer("reserved")
        self.assertFalse(can_ext_res)
        self.assertIn("صفقة", reason_res)

        can_ext_neg, reason_neg = can_extend_offer("negotiating")
        self.assertFalse(can_ext_neg)
        self.assertIn("تفاوض", reason_neg)

        for invalid in ("cancelled", "expired", "completed"):
            can_ext_inv, _ = can_extend_offer(invalid)
            self.assertFalse(can_ext_inv)

    def test_can_delete_offer(self):
        # Allowed statuses
        can_del_active, _ = can_delete_offer("active")
        self.assertTrue(can_del_active)
        can_del_grace, _ = can_delete_offer("expired_grace")
        self.assertTrue(can_del_grace)

        # Disallowed statuses
        can_del_res, _ = can_delete_offer("reserved")
        self.assertFalse(can_del_res)
        can_del_neg, _ = can_delete_offer("negotiating")
        self.assertFalse(can_del_neg)
        for invalid in ("cancelled", "expired", "completed"):
            can_del_inv, _ = can_delete_offer(invalid)
            self.assertFalse(can_del_inv)

    def test_negotiation_rejections(self):
        self.assertTrue(can_maker_reject_negotiation("PENDING_MAKER"))
        self.assertFalse(can_maker_reject_negotiation("ACCEPTED"))
        self.assertFalse(can_maker_reject_negotiation("EXPIRED"))

        self.assertTrue(can_taker_reject_negotiation("PENDING_TAKER"))
        self.assertFalse(can_taker_reject_negotiation("ACCEPTED"))
        self.assertFalse(can_taker_reject_negotiation("PENDING_MAKER"))

    def test_can_maker_counter_negotiation(self):
        self.assertTrue(can_maker_counter_negotiation("PENDING_MAKER", self.future, self.now))
        # Race condition: expired before submit
        self.assertFalse(can_maker_counter_negotiation("PENDING_MAKER", self.past, self.now))
        # Race condition: already accepted or rejected
        self.assertFalse(can_maker_counter_negotiation("ACCEPTED", self.future, self.now))
        self.assertFalse(can_maker_counter_negotiation("EXPIRED", self.future, self.now))

    def test_resolve_deal_parties(self):
        # When maker is seller: maker_id is returned, taker_id is buyer_id
        maker, taker = resolve_deal_parties(maker_id=1001, seller_id=1001, buyer_id=2002)
        self.assertEqual(maker, 1001)
        self.assertEqual(taker, 2002)

        # When maker is buyer: maker_id is returned, taker_id is seller_id
        maker, taker = resolve_deal_parties(maker_id=2002, seller_id=1001, buyer_id=2002)
        self.assertEqual(maker, 2002)
        self.assertEqual(taker, 1001)

        # When maker_id is None (e.g. offer deleted/not found)
        maker, taker = resolve_deal_parties(maker_id=None, seller_id=1001, buyer_id=2002)
        self.assertIsNone(maker)
        self.assertIsNone(taker)

    def test_can_cancel_deal(self):
        admin_ids = [8888]
        # Taker canceling when PENDING_OWNER_APPROVAL (maker is 1001, taker is 2002)
        allowed, _ = can_cancel_deal(
            deal_status="PENDING_OWNER_APPROVAL",
            user_id=2002,
            seller_id=1001,
            buyer_id=2002,
            offer_owner_id=1001,
            admin_ids=admin_ids,
        )
        self.assertTrue(allowed)

        # Non-party cannot cancel
        allowed, msg = can_cancel_deal(
            deal_status="PENDING_OWNER_APPROVAL",
            user_id=9999,
            seller_id=1001,
            buyer_id=2002,
            offer_owner_id=1001,
            admin_ids=admin_ids,
        )
        self.assertFalse(allowed)
        self.assertIn("غير مصرح", msg)

        # Offer owner (maker 1001) attempting to cancel in CREATED / AWAITING_USDT_DEPOSIT is rejected
        allowed, msg = can_cancel_deal(
            deal_status="AWAITING_USDT_DEPOSIT",
            user_id=1001,
            seller_id=1001,
            buyer_id=2002,
            offer_owner_id=1001,
            admin_ids=admin_ids,
        )
        self.assertFalse(allowed)
        self.assertIn("موافقتك", msg)

        # Taker (2002) allowed to cancel in AWAITING_USDT_DEPOSIT
        allowed, _ = can_cancel_deal(
            deal_status="AWAITING_USDT_DEPOSIT",
            user_id=2002,
            seller_id=1001,
            buyer_id=2002,
            offer_owner_id=1001,
            admin_ids=admin_ids,
        )
        self.assertTrue(allowed)

        # Admin canceling when CREATED
        allowed, _ = can_cancel_deal(
            deal_status="CREATED",
            user_id=8888,
            seller_id=1001,
            buyer_id=2002,
            offer_owner_id=1001,
            admin_ids=admin_ids,
        )
        self.assertTrue(allowed)

        # Cannot cancel once USDT deposited
        allowed, msg = can_cancel_deal(
            deal_status="USDT_DEPOSITED",
            user_id=2002,
            seller_id=1001,
            buyer_id=2002,
            offer_owner_id=1001,
            admin_ids=admin_ids,
        )
        self.assertFalse(allowed)

    def test_can_seller_confirm_syp(self):
        # Valid seller confirming when SYP_PAYMENT_SENT
        allowed, _ = can_seller_confirm_syp("SYP_PAYMENT_SENT", user_id=1001, seller_id=1001)
        self.assertTrue(allowed)

        # Buyer cannot confirm
        allowed, msg = can_seller_confirm_syp("SYP_PAYMENT_SENT", user_id=2002, seller_id=1001)
        self.assertFalse(allowed)
        self.assertIn("بائع", msg)

        # Already confirmed
        allowed, msg = can_seller_confirm_syp("ADMIN_APPROVAL", user_id=1001, seller_id=1001)
        self.assertFalse(allowed)

    def test_can_admin_release_escrow(self):
        admin_ids = [5001, 5002]
        # Admin releasing in ADMIN_APPROVAL status
        allowed, _ = can_admin_release_escrow("ADMIN_APPROVAL", user_id=5001, admin_ids=admin_ids)
        self.assertTrue(allowed)

        # Non-admin
        allowed, msg = can_admin_release_escrow("ADMIN_APPROVAL", user_id=1001, admin_ids=admin_ids)
        self.assertFalse(allowed)
        self.assertIn("للإدارة فقط", msg)

        # Wrong status
        allowed, msg = can_admin_release_escrow("SYP_PAYMENT_SENT", user_id=5001, admin_ids=admin_ids)
        self.assertFalse(allowed)

    def test_get_next_deal_status_after_syp_confirmed(self):
        # Normal flow returns ADMIN_APPROVAL
        self.assertEqual(get_next_deal_status_after_syp_confirmed("SYP_PAYMENT_SENT"), "ADMIN_APPROVAL")
        # Idempotent from SYP_PAYMENT_CONFIRMED
        self.assertEqual(get_next_deal_status_after_syp_confirmed("SYP_PAYMENT_CONFIRMED"), "ADMIN_APPROVAL")
        # Any other status returns None
        self.assertIsNone(get_next_deal_status_after_syp_confirmed("CREATED"))
        self.assertIsNone(get_next_deal_status_after_syp_confirmed("COMPLETED"))

    def test_seller_info_allowed_statuses(self):
        self.assertEqual(SELLER_INFO_ALLOWED_STATUSES, ("USDT_DEPOSITED",))
        self.assertNotIn("AWAITING_USDT_DEPOSIT", SELLER_INFO_ALLOWED_STATUSES)
        self.assertNotIn("AWAITING_SYP_PAYMENT", SELLER_INFO_ALLOWED_STATUSES)

    def test_should_release_orphan_offer(self):
        eleven_mins_ago = self.now - timedelta(minutes=11)
        five_mins_ago = self.now - timedelta(minutes=5)

        # Reserved offer with no live deal or negotiation and >10m elapsed -> release
        self.assertTrue(should_release_orphan_offer("reserved", False, False, eleven_mins_ago, self.now))

        # Negotiating offer with no live deal or negotiation and >10m elapsed -> release
        self.assertTrue(should_release_orphan_offer("negotiating", False, False, eleven_mins_ago, self.now))

        # Active offer -> do not release
        self.assertFalse(should_release_orphan_offer("active", False, False, eleven_mins_ago, self.now))

        # Has live deal -> do not release
        self.assertFalse(should_release_orphan_offer("reserved", True, False, eleven_mins_ago, self.now))

        # Has live negotiation -> do not release
        self.assertFalse(should_release_orphan_offer("negotiating", False, True, eleven_mins_ago, self.now))

        # Elapsed time less than 10 mins -> do not release
        self.assertFalse(should_release_orphan_offer("reserved", False, False, five_mins_ago, self.now))

        # status_changed_at is None -> do not release
        self.assertFalse(should_release_orphan_offer("reserved", False, False, None, self.now))

    def test_can_user_interact_with_offer(self):
        # Same user -> forbidden
        can_int, msg = can_user_interact_with_offer(offer_user_id=1001, user_id=1001)
        self.assertFalse(can_int)
        self.assertIn("بنفسك", msg)

        # Different user -> allowed
        can_int, msg = can_user_interact_with_offer(offer_user_id=1001, user_id=2002)
        self.assertTrue(can_int)
        self.assertEqual(msg, "")


if __name__ == "__main__":
    unittest.main()
