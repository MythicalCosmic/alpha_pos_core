"""Loyalty stamp engine.

Customers earn stamps when their orders complete + are paid. Once they hit
the per-reward threshold, a cashier can redeem a reward at the till and the
counter decrements. The bot's /loyalty command surfaces the current count.

Design notes:

- Accrual is keyed by phone_number (not chat_id) because orders only know
  about the phone the customer gave at order time. TelegramCustomer.phone
  is the link between the two after /login.
- Accrual is idempotent via OrderLoyaltyCredit: an order can hit the hook
  from status→COMPLETED or from mark_as_paid (when COMPLETED already), and
  we must not double-credit. A unique row per order_id is the guard.
- Settings are a singleton (LoyaltySettings) tuned per-deployment.
"""
import logging

from django.db import IntegrityError, transaction

from notifications.models import (
    LoyaltyAccount, LoyaltyRedemption, LoyaltySettings, OrderLoyaltyCredit,
)

logger = logging.getLogger(__name__)


def _normalize_phone(phone):
    """Strip whitespace and a single leading '+' for the lookup key.

    Telegram returns "998901234567" with no '+'; cashiers often type
    "+998…". We store the digits-only form so both sides agree.
    """
    if not phone:
        return ''
    phone = phone.strip()
    if phone.startswith('+'):
        phone = phone[1:]
    return phone[:20]


def maybe_accrue(order):
    """Credit stamps for `order` if eligible and not already credited.

    Eligibility: loyalty enabled, order status COMPLETED, is_paid True,
    has a phone_number. Idempotent via OrderLoyaltyCredit.
    """
    settings = LoyaltySettings.load()
    if not settings.is_enabled:
        return None
    if order.status != 'COMPLETED' or not order.is_paid:
        return None

    phone = _normalize_phone(order.phone_number)
    if not phone:
        return None

    stamps = settings.stamps_per_completed_order
    if stamps <= 0:
        return None

    try:
        with transaction.atomic():
            # Insert the credit row first; if a concurrent caller already
            # credited this order, IntegrityError fires and we bail out
            # without touching the balance.
            OrderLoyaltyCredit.objects.create(
                order_id=order.id, phone_number=phone, stamps_credited=stamps,
            )
            LoyaltyAccount.objects.get_or_create(phone_number=phone)
            # Apply the increment in SQL — read-modify-write in Python would
            # lose stamps when two different orders for the same phone
            # complete concurrently (both pass the unique-on-order_id guard
            # above, then race on `stamps_balance`).
            from django.db.models import F
            LoyaltyAccount.objects.filter(phone_number=phone).update(
                stamps_balance=F('stamps_balance') + stamps,
                stamps_earned_total=F('stamps_earned_total') + stamps,
            )
            return LoyaltyAccount.objects.get(phone_number=phone)
    except IntegrityError:
        logger.info('Loyalty already credited for order %s', order.id)
        return None


def get_account(phone):
    phone = _normalize_phone(phone)
    if not phone:
        return None
    try:
        return LoyaltyAccount.objects.get(phone_number=phone)
    except LoyaltyAccount.DoesNotExist:
        return None


def redeem(phone, cashier_id=None, order_id=None):
    """Spend one reward's worth of stamps for `phone`.

    Returns the updated LoyaltyAccount, or None if the customer doesn't
    have enough stamps / no account exists. The caller is expected to
    actually deliver the reward (free item, discount) at the till — this
    just moves the counter and writes a LoyaltyRedemption ledger row so the
    spend can be reconciled or disputed later.
    """
    settings = LoyaltySettings.load()
    cost = settings.stamps_per_reward
    if cost <= 0:
        return None

    phone = _normalize_phone(phone)
    with transaction.atomic():
        try:
            account = LoyaltyAccount.objects.select_for_update().get(
                phone_number=phone,
            )
        except LoyaltyAccount.DoesNotExist:
            return None
        if account.stamps_balance < cost:
            return None
        account.stamps_balance -= cost
        account.stamps_redeemed_total += cost
        account.save(update_fields=[
            'stamps_balance', 'stamps_redeemed_total', 'updated_at',
        ])
        LoyaltyRedemption.objects.create(
            phone_number=phone,
            stamps_spent=cost,
            cashier_id=cashier_id,
            order_id=order_id,
        )
        return account
