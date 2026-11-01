from aiogram.fsm.state import State, StatesGroup

class OfferCreateFSM(StatesGroup):
    waiting_for_type = State()
    waiting_for_rate = State()
    confirm_rate = State()
    waiting_for_min_limit = State()
    confirm_min_limit = State()
    waiting_for_max_limit = State()
    confirm_max_limit = State()
    waiting_for_methods = State()
    waiting_for_payout_details = State()
    confirm_payout_details = State()
    waiting_for_negotiable_decision = State()
    waiting_for_negotiable_type = State()
    waiting_for_duration_choice = State()
    waiting_for_custom_duration = State()
    confirm_custom_duration = State()
    waiting_for_confirm_publish = State()
    confirm_cancel = State()

class DealCreateFSM(StatesGroup):
    waiting_for_amount = State()
    waiting_for_payment_method = State()
    waiting_for_payout_address = State()
    waiting_for_seller_payment_info = State()

class NegotiationFSM(StatesGroup):
    waiting_for_taker_amount = State()
    waiting_for_taker_rate = State()
    waiting_for_maker_counter_amount = State()
    waiting_for_maker_counter_rate = State()

class DealPaymentFSM(StatesGroup):
    waiting_for_seller_account = State()
    waiting_for_buyer_wallet = State()
    waiting_for_syp_proof_photo = State()

class FeedbackFSM(StatesGroup):
    waiting_for_feedback_text = State()

class AdminFSM(StatesGroup):
    waiting_for_user_search_id = State()
    waiting_for_broadcast_text = State()

class ProfileFSM(StatesGroup):
    waiting_for_nickname = State()
    waiting_for_usdt_address = State()
    waiting_for_payment_info = State()


