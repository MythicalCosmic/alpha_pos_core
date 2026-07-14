from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.db.models import Sum, Count
from django.utils import timezone
from discounts.repositories import (
    DiscountRepository, DiscountTypeRepository,
    OrderDiscountRepository, DiscountUsageRepository,
)
from discounts.models import DiscountType, Discount
from base.repositories import OrderRepository
from base.helpers.response import ServiceResponse


def _serialize_discount(discount):
    # secret_word is intentionally NOT included in the public serialization to
    # prevent leaking the codeword to anyone with admin list access.
    data = {
        'id': discount.id,
        'uuid': str(discount.uuid),
        'name': discount.name,
        'code': discount.code,
        'description': discount.description,
        'value': str(discount.value),
        'min_order_amount': str(discount.min_order_amount) if discount.min_order_amount else None,
        'is_staff_only': discount.is_staff_only,
        'max_discount_amount': str(discount.max_discount_amount) if discount.max_discount_amount else None,
        'applies_to': discount.applies_to,
        'target_product_ids': discount.target_product_ids,
        'target_category_ids': discount.target_category_ids,
        'buy_quantity': discount.buy_quantity,
        'get_quantity': discount.get_quantity,
        'free_product_id': discount.free_product_id,
        'has_secret_word': bool(discount.secret_word),
        'usage_limit': discount.usage_limit,
        'usage_per_user': discount.usage_per_user,
        'usage_count': discount.usage_count,
        'start_date': discount.start_date.isoformat() if discount.start_date else None,
        'end_date': discount.end_date.isoformat() if discount.end_date else None,
        'is_stackable': discount.is_stackable,
        'is_active': discount.is_active,
        'created_by_id': discount.created_by_id,
        'created_at': discount.created_at.isoformat() if discount.created_at else None,
        'updated_at': discount.updated_at.isoformat() if discount.updated_at else None,
    }

    if hasattr(discount, '_discount_type_cache') or discount.discount_type_id:
        try:
            dt = discount.discount_type
            data['discount_type'] = {
                'id': dt.id,
                'uuid': str(dt.uuid),
                'name': dt.name,
                'code': dt.code,
                'discount_method': dt.discount_method,
                'discount_method_display': dt.get_discount_method_display(),
            }
        except DiscountType.DoesNotExist:
            data['discount_type'] = None
    else:
        data['discount_type'] = None

    if discount.free_product:
        data['free_product'] = {
            'id': discount.free_product.id,
            'name': discount.free_product.name,
            'price': str(discount.free_product.price),
        }
    else:
        data['free_product'] = None

    return data


class DiscountService:

    @staticmethod
    def list(page=1, per_page=20, discount_type_id=None, is_active=None, search=None):
        if discount_type_id:
            queryset = DiscountRepository.filter_by_type(discount_type_id)
        else:
            queryset = DiscountRepository.get_all()

        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)

        if search:
            queryset = DiscountRepository.search(queryset, search)

        queryset = queryset.select_related(
            'discount_type', 'free_product', 'created_by',
        ).order_by('-created_at')

        page_obj, paginator = DiscountRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            'discounts': [_serialize_discount(d) for d in page_obj.object_list],
            'pagination': {
                'current_page': page_obj.number,
                'total_pages': paginator.num_pages,
                'total_items': paginator.count,
                'per_page': per_page,
                'has_next': page_obj.has_next(),
                'has_previous': page_obj.has_previous(),
            },
        })

    @staticmethod
    def get(discount_id):
        discount = DiscountRepository.get_with_relations(discount_id)
        if not discount:
            return ServiceResponse.not_found("Discount not found")

        return ServiceResponse.success(data={
            'discount': _serialize_discount(discount),
        })

    @staticmethod
    @transaction.atomic
    def create(**kwargs):
        code = kwargs.get('code', '').strip()
        name = kwargs.get('name', '').strip()

        if not name:
            return ServiceResponse.validation_error(
                errors={'name': 'Name is required'},
                message='Name is required',
            )

        if not code:
            return ServiceResponse.validation_error(
                errors={'code': 'Code is required'},
                message='Code is required',
            )

        if DiscountRepository.code_exists(code):
            return ServiceResponse.error("Discount with this code already exists")

        discount_type_id = kwargs.get('discount_type_id')
        if not discount_type_id:
            return ServiceResponse.validation_error(
                errors={'discount_type_id': 'Discount type is required'},
                message='Discount type is required',
            )

        dt = DiscountTypeRepository.get_by_id(discount_type_id)
        if not dt:
            return ServiceResponse.not_found("Discount type not found")

        kwargs['name'] = name
        kwargs['code'] = code
        kwargs['discount_type'] = dt
        kwargs.pop('discount_type_id', None)

        # Reject negative money fields before they reach the model. A negative
        # `value` would invert the discount into a surcharge; negative bounds
        # are meaningless.
        for money_field in ('value', 'min_order_amount', 'max_discount_amount'):
            raw = kwargs.get(money_field)
            if raw is None or raw == '':
                continue
            try:
                if Decimal(str(raw)) < 0:
                    return ServiceResponse.validation_error(
                        errors={money_field: 'Must be zero or greater'},
                        message=f'{money_field} cannot be negative',
                    )
            except (InvalidOperation, TypeError, ValueError):
                return ServiceResponse.validation_error(
                    errors={money_field: 'Must be a number'},
                    message=f'{money_field} must be a number',
                )

        # Allowlist the fields a client may set on create. Without this the
        # raw request body is splatted into the model, letting a caller set
        # server-managed fields (usage_count, created_by_id, is_active, …) —
        # mass-assignment. Mirrors the allowlist in update().
        allowed_fields = {
            'name', 'code', 'description', 'value', 'min_order_amount',
            'is_staff_only', 'max_discount_amount', 'applies_to', 'target_product_ids',
            'target_category_ids', 'buy_quantity', 'get_quantity',
            'free_product', 'free_product_id', 'secret_word', 'usage_limit',
            'usage_per_user', 'start_date', 'end_date', 'is_stackable',
            'is_active', 'discount_type',
        }
        create_kwargs = {k: v for k, v in kwargs.items() if k in allowed_fields}

        discount = DiscountRepository.create(**create_kwargs)

        discount = DiscountRepository.get_with_relations(discount.id)

        return ServiceResponse.created(
            data={'discount': _serialize_discount(discount)},
            message="Discount created successfully",
        )

    @staticmethod
    @transaction.atomic
    def update(discount_id, **kwargs):
        discount = DiscountRepository.get_with_relations(discount_id)
        if not discount:
            return ServiceResponse.not_found("Discount not found")

        if 'code' in kwargs and kwargs['code']:
            code = kwargs['code'].strip()
            if DiscountRepository.code_exists(code, exclude_id=discount_id):
                return ServiceResponse.error("Discount with this code already exists")
            kwargs['code'] = code

        if 'name' in kwargs and kwargs['name']:
            kwargs['name'] = kwargs['name'].strip()

        if 'discount_type_id' in kwargs:
            dt = DiscountTypeRepository.get_by_id(kwargs['discount_type_id'])
            if not dt:
                return ServiceResponse.not_found("Discount type not found")
            kwargs['discount_type'] = dt
            kwargs.pop('discount_type_id')

        allowed_fields = {
            'name', 'code', 'description', 'value', 'min_order_amount',
            'is_staff_only', 'max_discount_amount', 'applies_to', 'target_product_ids',
            'target_category_ids', 'buy_quantity', 'get_quantity',
            'free_product', 'free_product_id', 'secret_word', 'usage_limit',
            'usage_per_user', 'start_date', 'end_date', 'is_stackable',
            'is_active', 'discount_type',
        }

        for key, value in kwargs.items():
            if key in allowed_fields and hasattr(discount, key):
                setattr(discount, key, value)

        discount.save()

        discount = DiscountRepository.get_with_relations(discount.id)

        return ServiceResponse.success(
            data={'discount': _serialize_discount(discount)},
            message="Discount updated successfully",
        )

    @staticmethod
    @transaction.atomic
    def delete(discount_id):
        discount = DiscountRepository.get_by_id(discount_id)
        if not discount:
            return ServiceResponse.not_found("Discount not found")

        discount.is_deleted = True
        discount.save(update_fields=['is_deleted', 'synced_at', 'sync_version'])

        return ServiceResponse.success(message="Discount deleted successfully")

    @staticmethod
    @transaction.atomic
    def toggle(discount_id):
        discount = DiscountRepository.get_by_id(discount_id)
        if not discount:
            return ServiceResponse.not_found("Discount not found")

        discount.is_active = not discount.is_active
        discount.save(update_fields=['is_active'])

        return ServiceResponse.success(
            data={'is_active': discount.is_active},
            message=f"Discount {'activated' if discount.is_active else 'deactivated'} successfully",
        )

    # ------------------------------------------------------------------
    # Core logic methods
    # ------------------------------------------------------------------

    @staticmethod
    def validate_code(code, order_subtotal=Decimal('0'), user_id=None):
        discount = DiscountRepository.get_by_code(code)
        if not discount:
            return ServiceResponse.not_found("Discount code not found")

        if not discount.is_active:
            return ServiceResponse.error("This discount is not active")

        if discount.is_deleted:
            return ServiceResponse.not_found("Discount code not found")

        now = timezone.now()

        if discount.start_date and discount.start_date > now:
            return ServiceResponse.error("This discount is not yet available")

        if discount.end_date and discount.end_date < now:
            return ServiceResponse.error("This discount has expired")

        if discount.usage_limit and discount.usage_count >= discount.usage_limit:
            return ServiceResponse.error("This discount has reached its usage limit")

        if discount.usage_per_user and user_id:
            user_usage = DiscountUsageRepository.count_for_user_discount(user_id, discount.id)
            if user_usage >= discount.usage_per_user:
                return ServiceResponse.error("You have reached the usage limit for this discount")

        if discount.min_order_amount and order_subtotal < discount.min_order_amount:
            return ServiceResponse.error(
                f"Minimum order amount of {discount.min_order_amount} is required"
            )

        return ServiceResponse.success(data={
            'discount': _serialize_discount(discount),
        })

    @staticmethod
    def calculate_discount(discount, order_items, already_applied_discount=Decimal('0')):
        method = discount.discount_type.discount_method

        # Determine which items the discount applies to
        if discount.applies_to == Discount.AppliesTo.SPECIFIC_PRODUCTS:
            applicable_items = [
                item for item in order_items
                if item.product_id in discount.target_product_ids
            ]
        elif discount.applies_to == Discount.AppliesTo.SPECIFIC_CATEGORIES:
            applicable_items = [
                item for item in order_items
                if item.product.category_id in discount.target_category_ids
            ]
        else:
            applicable_items = list(order_items)

        applicable_subtotal = sum(
            item.price * item.quantity for item in applicable_items
        )

        discount_amount = Decimal('0')

        if method == DiscountType.Method.PERCENTAGE:
            discount_amount = applicable_subtotal * (discount.value / Decimal('100'))
            if discount.max_discount_amount:
                discount_amount = min(discount_amount, discount.max_discount_amount)

        elif method == DiscountType.Method.FIXED_AMOUNT:
            discount_amount = min(discount.value, applicable_subtotal)

        elif method == DiscountType.Method.BUY_X_GET_Y:
            if applicable_items and discount.buy_quantity > 0:
                total_qty = sum(item.quantity for item in applicable_items)
                if total_qty >= discount.buy_quantity:
                    sets = total_qty // (discount.buy_quantity + discount.get_quantity)
                    free_qty = sets * discount.get_quantity
                    # Calculate value of cheapest items as free
                    prices = []
                    for item in applicable_items:
                        prices.extend([item.price] * item.quantity)
                    prices.sort()
                    discount_amount = sum(prices[:free_qty])

        elif method == DiscountType.Method.FREE_ITEM:
            if discount.free_product_id:
                for item in order_items:
                    if item.product_id == discount.free_product_id:
                        discount_amount = item.price
                        break

        elif method == DiscountType.Method.SECRET_WORD:
            # Secret word discounts work as percentage or fixed based on value
            if discount.value <= Decimal('100') and discount.value > Decimal('0'):
                discount_amount = applicable_subtotal * (discount.value / Decimal('100'))
                if discount.max_discount_amount:
                    discount_amount = min(discount_amount, discount.max_discount_amount)
            else:
                discount_amount = min(discount.value, applicable_subtotal)

        elif method == DiscountType.Method.CUSTOM:
            discount_amount = applicable_subtotal * (discount.value / Decimal('100'))
            if discount.max_discount_amount:
                discount_amount = min(discount_amount, discount.max_discount_amount)

        # Final clamp: the discount can never exceed the applicable subtotal.
        # Guards against badly-configured rules (e.g. a 150% PERCENTAGE that
        # bypassed model validation, or FIXED_AMOUNT > subtotal). Without
        # this, apply_to_order could write total_amount < 0 and mark_as_paid
        # would call add_to_register with a negative value.
        applicable_subtotal_dec = Decimal(str(applicable_subtotal or 0))
        if discount_amount > applicable_subtotal_dec:
            discount_amount = applicable_subtotal_dec
        if discount_amount < Decimal('0'):
            discount_amount = Decimal('0')

        # Cumulative cap: a single rule is capped at its own applicable
        # subtotal above, but when discounts stack the SUM can still exceed the
        # whole order subtotal (only a downstream max(0, ...) hides it). Clamp
        # this rule's contribution to the order subtotal remaining after the
        # discounts already applied so the running total never goes past it.
        order_subtotal = Decimal(str(
            sum(item.price * item.quantity for item in order_items) or 0
        ))
        already_applied = Decimal(str(already_applied_discount or 0))
        remaining_subtotal = order_subtotal - already_applied
        if remaining_subtotal < Decimal('0'):
            remaining_subtotal = Decimal('0')
        if discount_amount > remaining_subtotal:
            discount_amount = remaining_subtotal

        return discount_amount.quantize(Decimal('0.01'))

    @staticmethod
    @transaction.atomic
    def apply_to_order(order_id, discount_code, user_id=None):
        # Validate the discount code
        result, status = DiscountService.validate_code(
            discount_code,
            order_subtotal=Decimal('0'),
            user_id=user_id,
        )
        if not result.get('success'):
            return result, status

        # Row-lock the order so a concurrent mark_as_paid can't read the
        # pre-discount total_amount while this transaction rewrites it.
        order = OrderRepository.get_for_update(order_id)
        if not order:
            return ServiceResponse.not_found("Order not found")

        if order.is_paid:
            return ServiceResponse.error("Cannot apply discount to a paid order")

        if order.status == 'CANCELED':
            return ServiceResponse.error("Cannot apply discount to a cancelled order")

        locked = Discount.objects.select_for_update().filter(
            code=discount_code, is_deleted=False
        ).first()
        if not locked:
            return ServiceResponse.not_found("Discount code not found")
        discount = locked

        # Re-check usage limits under the row lock — validate_code's earlier
        # read was unlocked, so two concurrent applies could both have passed.
        if discount.usage_limit and discount.usage_count >= discount.usage_limit:
            return ServiceResponse.error("This discount has reached its usage limit")

        if discount.usage_per_user and user_id:
            user_usage = DiscountUsageRepository.count_for_user_discount(user_id, discount.id)
            if user_usage >= discount.usage_per_user:
                return ServiceResponse.error("You have reached the usage limit for this discount")

        # Re-validate with actual order subtotal
        if discount.min_order_amount and order.subtotal < discount.min_order_amount:
            return ServiceResponse.error(
                f"Minimum order amount of {discount.min_order_amount} is required"
            )

        # Staff-only discount: only an order attributed to an is_staff customer
        # qualifies (employee personal orders). Open discounts skip this.
        if discount.is_staff_only and not (order.customer and order.customer.is_staff):
            return ServiceResponse.error("This discount is available to staff customers only")

        # Check if discount already applied
        existing = OrderDiscountRepository.get_for_order(order_id).filter(
            discount_id=discount.id,
        )
        if existing.exists():
            return ServiceResponse.error("This discount is already applied to the order")

        # Check stackability
        if not discount.is_stackable:
            order_discounts = OrderDiscountRepository.get_for_order(order_id)
            if order_discounts.exists():
                return ServiceResponse.error(
                    "This discount cannot be combined with other discounts"
                )

        # Also check if existing discounts are non-stackable
        existing_non_stackable = OrderDiscountRepository.get_for_order(order_id).filter(
            discount__is_stackable=False,
        )
        if existing_non_stackable.exists():
            return ServiceResponse.error(
                "Order already has a non-stackable discount applied"
            )

        # Calculate the discount amount. Pass the discount already applied to
        # this order so calculate_discount clamps this rule to the remaining
        # subtotal — stacked discounts can't sum past the order subtotal.
        order_items = order.items.filter(is_deleted=False).select_related(
            'product__category',
        )
        already_applied = OrderDiscountRepository.get_for_order(order_id).aggregate(
            total=Sum('discount_amount'),
        )['total'] or Decimal('0')
        discount_amount = DiscountService.calculate_discount(
            discount, order_items, already_applied_discount=already_applied,
        )

        if discount_amount <= 0:
            return ServiceResponse.error("Discount does not apply to this order")

        # Create OrderDiscount
        order_discount = OrderDiscountRepository.create(
            order=order,
            discount=discount,
            discount_code=discount.code,
            discount_amount=discount_amount,
            applied_by_id=user_id,
            branch_id=order.branch_id,
        )

        # Create DiscountUsage
        if user_id:
            DiscountUsageRepository.create(
                discount=discount,
                user_id=user_id,
                order=order,
                branch_id=order.branch_id,
            )

        # The discount row is already locked above, so an instance save is both
        # concurrency-safe and sync-visible. QuerySet.update() left the peer's
        # usage limit stale, allowing overuse on another surface.
        discount.usage_count += 1
        discount.save(update_fields=['usage_count'])

        # Recalculate order totals
        total_discount = OrderDiscountRepository.get_for_order(order_id).aggregate(
            total=Sum('discount_amount'),
        )['total'] or Decimal('0')

        order.discount_amount = total_discount
        # Clamp at zero — defensive, since calculate_discount already caps
        # per-rule, but multiple stackable discounts could in theory sum
        # past subtotal.
        order.total_amount = max(Decimal('0'), order.subtotal - order.discount_amount)
        order.save(update_fields=['discount_amount', 'total_amount'])

        return ServiceResponse.success(
            data={
                'order_discount_id': order_discount.id,
                'discount_code': discount.code,
                'discount_amount': str(discount_amount),
                'order_total': str(order.total_amount),
                'order_discount_total': str(order.discount_amount),
            },
            message="Discount applied successfully",
        )

    @staticmethod
    @transaction.atomic
    def remove_from_order(order_id, order_discount_id, user_id=None):
        # Row-lock the order so a concurrent mark_as_paid can't read the
        # discounted total while this transaction is restoring it.
        order = OrderRepository.get_for_update(order_id)
        if not order:
            return ServiceResponse.not_found("Order not found")

        # Removing a discount from a paid order raises `total_amount` but
        # has no path to also bump the cash register, so the drawer would
        # under-report by the discount amount. The legitimate "fix a paid
        # order" flow is cancel-and-reissue, which already reverses cash.
        if order.is_paid:
            return ServiceResponse.error(
                "Cannot remove discount from a paid order — cancel and reissue instead",
            )

        order_discount = OrderDiscountRepository.get_with_relations(order_discount_id)
        if not order_discount or order_discount.order_id != order_id:
            return ServiceResponse.not_found("Order discount not found")

        # Serialize usage counter changes across different orders and reload
        # the current value rather than decrementing a stale related instance.
        discount = Discount.objects.select_for_update().get(
            pk=order_discount.discount_id,
        )

        # Delete the DiscountUsage tied to THIS order + discount, regardless of
        # which user removes it. Filtering by the caller-supplied user_id (e.g.
        # an admin removing a discount a customer applied) orphaned the usage
        # row while usage_count was still decremented below — drifting the
        # per-user and global counters out of sync. Apply created exactly one
        # usage row per (order, discount), so deleting by order+discount keeps
        # the counters consistent on apply/remove for the same order.
        usages = DiscountUsageRepository.get_for_order(order_id).filter(
            discount_id=discount.id,
        )
        # QuerySet.delete() bypasses SyncMixin tombstones. Keep synced soft
        # deletes so the peer also releases the per-user allowance.
        for usage in usages.select_for_update():
            usage.delete()

        # Delete the OrderDiscount
        order_discount.delete()

        # The row lock makes this read-modify-write atomic; save() also marks
        # the SyncMixin dirty so every node sees the restored allowance.
        discount.usage_count = max(discount.usage_count - 1, 0)
        discount.save(update_fields=['usage_count'])

        # Recalculate order totals
        total_discount = OrderDiscountRepository.get_for_order(order_id).aggregate(
            total=Sum('discount_amount'),
        )['total'] or Decimal('0')

        order.discount_amount = total_discount
        # Clamp at zero — defensive, since calculate_discount already caps
        # per-rule, but multiple stackable discounts could in theory sum
        # past subtotal.
        order.total_amount = max(Decimal('0'), order.subtotal - order.discount_amount)
        order.save(update_fields=['discount_amount', 'total_amount'])

        return ServiceResponse.success(
            data={
                'order_total': str(order.total_amount),
                'order_discount_total': str(order.discount_amount),
            },
            message="Discount removed successfully",
        )

    @staticmethod
    @transaction.atomic
    def validate_secret_word(word, order_id, user_id=None):
        # Find active SECRET_WORD discounts matching the word
        discounts = Discount.objects.filter(
            is_deleted=False,
            is_active=True,
            discount_type__discount_method=DiscountType.Method.SECRET_WORD,
            secret_word__iexact=word,
        ).select_related('discount_type', 'free_product')

        discount = discounts.first()
        if not discount:
            return ServiceResponse.error("Invalid secret word")

        # Apply the found discount to the order
        return DiscountService.apply_to_order(order_id, discount.code, user_id)

    @staticmethod
    def get_stats(discount_id):
        discount = DiscountRepository.get_with_relations(discount_id)
        if not discount:
            return ServiceResponse.not_found("Discount not found")

        total_uses = DiscountUsageRepository.count_for_discount(discount_id)

        revenue_impact = OrderDiscountRepository.model.objects.filter(
            is_deleted=False, discount_id=discount_id,
        ).aggregate(
            total_discount_given=Sum('discount_amount'),
            order_count=Count('order_id', distinct=True),
        )

        top_users = list(
            DiscountUsageRepository.model.objects.filter(
                is_deleted=False, discount_id=discount_id,
            ).values(
                'user_id', 'user__first_name', 'user__last_name',
            ).annotate(
                use_count=Count('id'),
            ).order_by('-use_count')[:10]
        )

        return ServiceResponse.success(data={
            'discount': _serialize_discount(discount),
            'stats': {
                'total_uses': total_uses,
                'total_discount_given': str(
                    revenue_impact['total_discount_given'] or Decimal('0')
                ),
                'order_count': revenue_impact['order_count'] or 0,
                'top_users': [
                    {
                        'user_id': u['user_id'],
                        'name': f"{u['user__first_name']} {u['user__last_name']}",
                        'use_count': u['use_count'],
                    }
                    for u in top_users
                ],
            },
        })
