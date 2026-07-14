from django.db import models
from base.models import SyncMixin, SyncManager


class DiscountType(SyncMixin, models.Model):
    SYNC_PULL_SCOPE = 'global'
    class Method(models.TextChoices):
        PERCENTAGE = 'PERCENTAGE', 'Percentage'
        FIXED_AMOUNT = 'FIXED_AMOUNT', 'Fixed Amount'
        BUY_X_GET_Y = 'BUY_X_GET_Y', 'Buy X Get Y'
        FREE_ITEM = 'FREE_ITEM', 'Free Item'
        SECRET_WORD = 'SECRET_WORD', 'Secret Word'
        CUSTOM = 'CUSTOM', 'Custom'

    name = models.CharField(max_length=100)
    code = models.SlugField(max_length=50, unique=True)
    description = models.TextField(blank=True, default='')
    discount_method = models.CharField(
        max_length=15, choices=Method.choices, default=Method.PERCENTAGE,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.get_discount_method_display()})"


class Discount(SyncMixin, models.Model):
    SYNC_PULL_SCOPE = 'global'
    class AppliesTo(models.TextChoices):
        ENTIRE_ORDER = 'ENTIRE_ORDER', 'Entire Order'
        SPECIFIC_PRODUCTS = 'SPECIFIC_PRODUCTS', 'Specific Products'
        SPECIFIC_CATEGORIES = 'SPECIFIC_CATEGORIES', 'Specific Categories'

    discount_type = models.ForeignKey(
        DiscountType, on_delete=models.CASCADE, related_name='discounts',
    )
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True, default='')
    value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    min_order_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )
    # Staff-only: this discount applies only to an order whose customer is
    # flagged is_staff (employee personal orders). Default False = open to all.
    is_staff_only = models.BooleanField(default=False)
    max_discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )
    applies_to = models.CharField(
        max_length=20, choices=AppliesTo.choices, default=AppliesTo.ENTIRE_ORDER,
    )
    target_product_ids = models.JSONField(default=list, blank=True)
    target_category_ids = models.JSONField(default=list, blank=True)
    buy_quantity = models.PositiveIntegerField(default=0)
    get_quantity = models.PositiveIntegerField(default=0)
    free_product = models.ForeignKey(
        'base.Product', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='free_in_discounts',
    )
    secret_word = models.CharField(max_length=100, blank=True, default='')
    usage_limit = models.PositiveIntegerField(null=True, blank=True)
    usage_per_user = models.PositiveIntegerField(null=True, blank=True)
    usage_count = models.PositiveIntegerField(default=0)
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    is_stackable = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='created_discounts',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-created_at']

    def clean(self):
        # Bound percentage / secret-word values at the model level so an
        # admin can't save a 150% rule that later computes
        # discount_amount = 1.5 * subtotal in calculate_discount.
        super().clean()
        from django.core.exceptions import ValidationError
        from decimal import Decimal
        method = getattr(getattr(self, 'discount_type', None), 'discount_method', None)
        percent_methods = {'PERCENTAGE', 'SECRET_WORD', 'CUSTOM'}
        if method in percent_methods:
            if self.value is not None and (
                self.value < Decimal('0') or self.value > Decimal('100')
            ):
                raise ValidationError(
                    {'value': 'For percentage-based discounts, value must be 0–100.'},
                )
        if self.value is not None and self.value < Decimal('0'):
            raise ValidationError({'value': 'Discount value cannot be negative.'})

    def save(self, *args, **kwargs):
        # Run model validation on every save so admin-bypassing code paths
        # (DRF, custom services) still hit the bound check.
        self.full_clean(exclude={'created_by', 'discount_type', 'free_product'})
        super().save(*args, **kwargs)

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['discount_type_uuid'] = str(self.discount_type.uuid) if self.discount_type else None
        data['free_product_uuid'] = str(self.free_product.uuid) if self.free_product else None
        data['created_by_uuid'] = str(self.created_by.uuid) if self.created_by else None
        return data

    def __str__(self):
        return f"{self.name} ({self.code})"


class OrderDiscount(SyncMixin, models.Model):
    order = models.ForeignKey(
        'base.Order', on_delete=models.CASCADE, related_name='applied_discounts',
    )
    discount = models.ForeignKey(
        Discount, on_delete=models.CASCADE, related_name='order_applications',
    )
    discount_code = models.CharField(max_length=50)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2)
    applied_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='applied_discounts',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-created_at']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['order_uuid'] = str(self.order.uuid) if self.order else None
        data['discount_uuid'] = str(self.discount.uuid) if self.discount else None
        data['applied_by_uuid'] = str(self.applied_by.uuid) if self.applied_by else None
        return data

    def __str__(self):
        return f"Discount {self.discount_code} on Order #{self.order_id}"


class DiscountUsage(SyncMixin, models.Model):
    discount = models.ForeignKey(
        Discount, on_delete=models.CASCADE, related_name='usages',
    )
    user = models.ForeignKey(
        'base.User', on_delete=models.CASCADE, related_name='discount_usages',
    )
    order = models.ForeignKey(
        'base.Order', on_delete=models.CASCADE, related_name='discount_usages',
    )
    used_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-used_at']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['discount_uuid'] = str(self.discount.uuid) if self.discount else None
        data['user_uuid'] = str(self.user.uuid) if self.user else None
        data['order_uuid'] = str(self.order.uuid) if self.order else None
        return data

    def __str__(self):
        return f"{self.user} used {self.discount.code}"
