from django.db import models

# The message categories a chat can be subscribed to / muted from, in the order
# the desktop panel lists them. Every real NotificationTemplate type is mapped
# onto one of these buckets by `bucket_for`. 'system' is the catch-all and also
# carries the background sync push/pull/error messages.
ROUTABLE_TYPES = ('order_paid', 'daily', 'contract', 'document', 'system')


def bucket_for(notification_type):
    """Map a raw NotificationTemplate type (e.g. 'order_paid', 'hr.contract_expiry')
    onto one of ROUTABLE_TYPES so per-chat routing can be expressed in a handful
    of human-meaningful categories."""
    nt = (notification_type or '').lower()
    if 'contract' in nt:
        return 'contract'
    if 'document' in nt:
        return 'document'
    if 'order' in nt or 'paid' in nt:
        return 'order_paid'
    if 'daily' in nt or 'summary' in nt or 'shift' in nt:
        return 'daily'
    return 'system'


class NotificationSettings(models.Model):
    bot_token = models.CharField(max_length=200, blank=True, default='')
    chat_ids = models.JSONField(default=list, blank=True)
    # Per-chat message routing + label, keyed by chat id:
    #   {"<cid>": {"label": "Owner", "events": {"order_paid": true, ...}}}
    # A chat (or an event) missing from the map defaults to RECEIVING that
    # category, so existing installs keep getting everything until an operator
    # narrows it. Operator-managed from the desktop panel; branch-local (this row
    # is a pinned singleton, not a SyncMixin) so it never propagates.
    chat_routing = models.JSONField(default=dict, blank=True)
    brand_name = models.CharField(max_length=100, default='Alpha POS')
    is_enabled = models.BooleanField(default=True)
    timeout = models.PositiveIntegerField(default=10)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'notification settings'
        verbose_name_plural = 'notification settings'

    _CACHE_KEY = 'notification_settings:v1'
    _CACHE_TTL = 60

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        from django.core.cache import cache
        cache.delete(self._CACHE_KEY)

    @classmethod
    def load(cls):
        # Cached singleton: every legacy TelegramAPI call (send_to_chat,
        # answer_callback_query, edit_message_text) and every modern
        # TelegramService send_message hits this row. Without the cache,
        # one Telegram /menu tap = 3+ SELECTs on a row that rarely changes.
        from django.core.cache import cache
        cached = cache.get(cls._CACHE_KEY)
        if cached is not None:
            return cached
        obj, created = cls.objects.get_or_create(pk=1)
        if created:
            # Seed from env on first creation so a packaged build's baked-in
            # Telegram defaults (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_IDS) take
            # effect on a fresh DB. The operator can still edit them in the panel
            # afterwards (that writes the DB row, which wins).
            import os
            token = (os.environ.get('TELEGRAM_BOT_TOKEN') or '').strip()
            raw_ids = (os.environ.get('TELEGRAM_CHAT_IDS') or '').strip()
            ids = [c.strip() for c in raw_ids.replace(';', ',').split(',') if c.strip()]
            if token or ids:
                obj.bot_token = token
                obj.chat_ids = ids
                obj.save()
        cache.set(cls._CACHE_KEY, obj, cls._CACHE_TTL)
        return obj

    def routing_for(self, chat_id):
        """Resolve a chat's per-category subscription, defaulting every missing
        entry to True (receive)."""
        entry = (self.chat_routing or {}).get(str(chat_id)) or {}
        events = entry.get('events') if isinstance(entry, dict) else None
        events = events if isinstance(events, dict) else {}
        return {tp: bool(events.get(tp, True)) for tp in ROUTABLE_TYPES}

    def recipients_for(self, message_type):
        """The configured chat ids that should receive `message_type` (one of
        ROUTABLE_TYPES, or a raw template type which is bucketed first)."""
        bucket = message_type if message_type in ROUTABLE_TYPES else bucket_for(message_type)
        return [str(c) for c in (self.chat_ids or [])
                if self.routing_for(c).get(bucket, True)]

    def __str__(self):
        return f"Notification Settings ({self.brand_name})"


class NotificationTemplate(models.Model):
    notification_type = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    template_text = models.TextField()
    # Free-text description listing which placeholders are valid for this
    # template type. Surfaced on the admin UI so editors don't have to read
    # the source to know what {variables} they can use.
    description = models.TextField(
        blank=True, default='',
        help_text='Document the available {placeholders} for this template.',
    )
    is_enabled = models.BooleanField(default=True)
    language = models.CharField(max_length=5, default='uz')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['notification_type']

    def __str__(self):
        return f"{self.name} ({self.notification_type})"


class TelegramCustomer(models.Model):
    """Customer-side Telegram account record.

    Created the first time a Telegram user opens the bot (`/start`).
    Optionally linked to a `base.User` once the customer authenticates
    inside the bot — pre-link, the row tracks the chat for greetings and
    order-status pushes only.

    Not a SyncMixin: chat-id↔user mapping is per-deployment and shouldn't
    propagate across branches.
    """

    chat_id = models.BigIntegerField(unique=True, db_index=True)
    user = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='telegram_customers',
    )
    first_name = models.CharField(max_length=64, blank=True, default='')
    last_name = models.CharField(max_length=64, blank=True, default='')
    username = models.CharField(max_length=64, blank=True, default='')
    language_code = models.CharField(max_length=8, blank=True, default='')
    # Saved when the user taps the request_contact button on /login.
    # Used to match TelegramCustomer ↔ existing Orders by phone_number,
    # and as the foundation for the upcoming loyalty linkage.
    phone_number = models.CharField(
        max_length=20, blank=True, default='', db_index=True,
    )
    # Set true when sendMessage returns 403 (user blocked the bot). Avoids
    # hammering Telegram with messages that will keep failing.
    is_blocked = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_seen_at']
        verbose_name = 'telegram customer'

    def __str__(self):
        label = self.username or self.first_name or str(self.chat_id)
        return f'TelegramCustomer<{label}>'


class LoyaltySettings(models.Model):
    """Singleton holding stamps-per-order and stamps-per-reward thresholds.

    `pk` is pinned to 1 in save() so there is exactly one row, mirroring the
    pattern used by NotificationSettings. An admin endpoint reads/writes
    this row; the loyalty service consults it on every accrual.
    """
    is_enabled = models.BooleanField(default=True)
    stamps_per_completed_order = models.PositiveIntegerField(default=1)
    stamps_per_reward = models.PositiveIntegerField(default=10)
    reward_description = models.CharField(
        max_length=120, default='Bepul ichimlik',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'loyalty settings'
        verbose_name_plural = 'loyalty settings'

    _CACHE_KEY = 'loyalty_settings:v1'
    _CACHE_TTL = 60

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        from django.core.cache import cache
        cache.delete(self._CACHE_KEY)

    @classmethod
    def load(cls):
        # Hit on every order paid + completed transition (accrual hook) and
        # every /loyalty bot command — cache the singleton.
        from django.core.cache import cache
        cached = cache.get(cls._CACHE_KEY)
        if cached is not None:
            return cached
        obj, _ = cls.objects.get_or_create(pk=1)
        cache.set(cls._CACHE_KEY, obj, cls._CACHE_TTL)
        return obj

    def __str__(self):
        return f'LoyaltySettings(per_order={self.stamps_per_completed_order}, per_reward={self.stamps_per_reward})'


class LoyaltyAccount(models.Model):
    """Per-customer stamp ledger, keyed by phone number.

    Phone is the link key (not chat_id) because the same person may have
    multiple Telegram clients (mobile + desktop) producing different chat_ids
    over time, and because orders are placed against a phone — not against a
    Telegram account. The bot looks up the account via TelegramCustomer.phone
    after /login.
    """
    phone_number = models.CharField(max_length=20, unique=True, db_index=True)
    stamps_balance = models.PositiveIntegerField(default=0)
    stamps_earned_total = models.PositiveIntegerField(default=0)
    stamps_redeemed_total = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'loyalty account'

    def __str__(self):
        return f'LoyaltyAccount<{self.phone_number}: {self.stamps_balance}>'


class OrderLoyaltyCredit(models.Model):
    """Idempotency record: which orders already credited loyalty stamps.

    An order can hit the accrual hook from two paths (status→COMPLETED and
    mark_as_paid when already COMPLETED). A unique row per order_id ensures
    a second pass is a silent no-op instead of double-crediting stamps.
    """
    order_id = models.IntegerField(unique=True, db_index=True)
    phone_number = models.CharField(max_length=20, db_index=True)
    stamps_credited = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'OrderLoyaltyCredit<order={self.order_id} +{self.stamps_credited}>'


class LoyaltyRedemption(models.Model):
    """Ledger row for each reward redeemed at the till.

    Mirrors OrderLoyaltyCredit on the accrual side: redemption moves the
    counter, so without a record there is nothing to reconcile or reverse if
    the physical reward isn't delivered. Optional order_id/cashier_id tie the
    spend to where it happened.
    """
    phone_number = models.CharField(max_length=20, db_index=True)
    stamps_spent = models.PositiveIntegerField()
    order_id = models.IntegerField(null=True, blank=True, db_index=True)
    cashier_id = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'LoyaltyRedemption<{self.phone_number} -{self.stamps_spent}>'


class Cart(models.Model):
    """One-active-cart-per-TelegramCustomer holding items in progress.

    The cart is the customer's draft order before checkout. We keep it
    server-side (rather than reconstructing from messages) so the customer
    can browse /menu in between adds without losing state. On /order
    checkout, the cart is converted to a real base.Order and the row stays
    for history (status=CHECKED_OUT) — never deleted, so we can recover
    if the resulting Order failed to persist.
    """
    class Status(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        CHECKED_OUT = 'CHECKED_OUT', 'Checked out'
        ABANDONED = 'ABANDONED', 'Abandoned'

    customer = models.ForeignKey(
        'notifications.TelegramCustomer',
        on_delete=models.CASCADE,
        related_name='carts',
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.ACTIVE,
        db_index=True,
    )
    # The base.Order this cart became, if checkout succeeded.
    order = models.ForeignKey(
        'base.Order', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='source_carts',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        constraints = [
            # Enforce exactly one ACTIVE cart per customer at DB level so a
            # race between two simultaneous /order add calls can't create
            # two parallel carts. The status field is part of the index so
            # checked-out carts (historical) don't clash.
            models.UniqueConstraint(
                fields=['customer'], condition=models.Q(status='ACTIVE'),
                name='one_active_cart_per_customer',
            ),
        ]

    def __str__(self):
        return f'Cart<customer={self.customer_id} status={self.status}>'


class CartItem(models.Model):
    """A line on a Cart. Price is snapshotted at add time so a mid-cart
    price change on the menu doesn't surprise the customer at checkout."""
    cart = models.ForeignKey(
        Cart, on_delete=models.CASCADE, related_name='items',
    )
    product = models.ForeignKey(
        'base.Product', on_delete=models.CASCADE,
    )
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            # One row per (cart, product). /order add bumps quantity on the
            # existing row instead of creating duplicates.
            models.UniqueConstraint(
                fields=['cart', 'product'], name='unique_product_per_cart',
            ),
        ]

    def __str__(self):
        return f'CartItem<{self.product_id} x{self.quantity}>'


class NotificationLog(models.Model):
    class Status(models.TextChoices):
        SENT = 'SENT', 'Sent'
        FAILED = 'FAILED', 'Failed'
        QUEUED = 'QUEUED', 'Queued'

    notification_type = models.CharField(max_length=50)
    recipient = models.CharField(max_length=50)
    message_text = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices)
    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.notification_type} -> {self.recipient} ({self.status})"


class OrderNotificationDispatch(models.Model):
    """Per-order staff-notification state (server-side, NOT synced).

    Drives idempotent firing of the staff order notifications as orders sync up
    from the tills (the server is the single notification source). `new_message_ids`
    stores the Telegram message id of the `order.new` message in each chat so the
    later `order.ready` message can be sent as a REPLY threaded under it.
    One row per base.Order id.
    """
    order_id = models.IntegerField(unique=True, db_index=True)
    new_sent = models.BooleanField(default=False)
    ready_sent = models.BooleanField(default=False)
    paid_sent = models.BooleanField(default=False)
    cancelled_sent = models.BooleanField(default=False)
    # {"<chat_id>": <telegram_message_id>} of the order.new message per chat.
    new_message_ids = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"OrderNotificationDispatch<order={self.order_id}>"
