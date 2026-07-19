from django.contrib import admin
from .models import (
    User, Session, Category, Product, DeliveryPerson, Place, Table,
    Order, OrderItem, CashRegister, Inkassa, AppSettings,
    ShiftTemplate, Shift, CashReconciliation,
)


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('id', 'email', 'first_name', 'last_name', 'role', 'status', 'last_login_at')
    list_filter = ('role', 'status')
    search_fields = ('email', 'first_name', 'last_name')
    ordering = ('-id',)

    def save_model(self, request, obj, form, change):
        # A plain ModelAdmin saves the `password` field verbatim — so a PIN typed
        # here ('2233') was stored as PLAINTEXT and login's check_password() could
        # never verify it (401 Invalid credentials). Hash any value that isn't
        # already a recognized Django hash, leaving real hashes untouched so
        # editing other fields doesn't re-hash.
        from django.contrib.auth.hashers import identify_hasher
        from base.security.hashing import hash_password
        pw = obj.password or ''
        already_hashed = False
        if pw:
            try:
                identify_hasher(pw)
                already_hashed = True
            except Exception:  # noqa: BLE001 — unrecognized => plaintext
                already_hashed = False
        if pw and not already_hashed:
            obj.password = hash_password(pw)
        super().save_model(request, obj, form, change)


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user_id', 'ip_address', 'last_activity')
    list_filter = ('last_activity',)
    raw_id_fields = ('user_id',)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'slug', 'status', 'sort_order', 'created_at')
    list_filter = ('status',)
    search_fields = ('name', 'slug')
    ordering = ('sort_order', 'name')


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'category', 'price', 'created_at')
    list_filter = ('category',)
    search_fields = ('name',)
    autocomplete_fields = ('category',)


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    autocomplete_fields = ('product',)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'display_id', 'user', 'cashier', 'order_type', 'status', 'is_paid', 'total_amount', 'created_at')
    list_filter = ('status', 'is_paid', 'order_type', 'created_at')
    search_fields = ('display_id', 'phone_number', 'description')
    autocomplete_fields = ('user', 'cashier', 'delivery_person', 'place', 'table')
    # These fields are a derived settlement header, not an admin checkbox.
    # Editing them directly bypasses OrderPayment, the active-shift guard,
    # drawer accounting, fiscalization and refund invariants. Corrections must
    # use the explicit pay/refund/repair services that write auditable evidence.
    readonly_fields = (
        'is_paid', 'payment_method', 'paid_at', 'accounting_recorded_at',
    )
    inlines = [OrderItemInline]
    date_hierarchy = 'created_at'


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'order', 'product', 'quantity', 'price', 'ready_at')
    autocomplete_fields = ('order', 'product')


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'place_type', 'capacity', 'is_active', 'sort_order')
    list_filter = ('place_type', 'is_active')
    search_fields = ('name',)


@admin.register(Table)
class TableAdmin(admin.ModelAdmin):
    list_display = ('id', 'place', 'number', 'capacity', 'status', 'is_active')
    list_filter = ('status', 'is_active', 'place')
    search_fields = ('number',)
    autocomplete_fields = ('place',)


@admin.register(DeliveryPerson)
class DeliveryPersonAdmin(admin.ModelAdmin):
    list_display = ('id', 'first_name', 'last_name', 'phone_number', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('first_name', 'last_name', 'phone_number')


@admin.register(CashRegister)
class CashRegisterAdmin(admin.ModelAdmin):
    list_display = ('id', 'current_balance', 'last_updated')


@admin.register(Inkassa)
class InkassaAdmin(admin.ModelAdmin):
    list_display = ('id', 'cashier', 'inkass_type', 'amount', 'balance_before', 'balance_after', 'created_at')
    list_filter = ('inkass_type', 'created_at')
    autocomplete_fields = ('cashier',)
    date_hierarchy = 'created_at'


@admin.register(AppSettings)
class AppSettingsAdmin(admin.ModelAdmin):
    list_display = ('id', 'hr_enabled', 'waiter_enabled', 'updated_at')


@admin.register(ShiftTemplate)
class ShiftTemplateAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'start_time', 'end_time', 'is_active')
    search_fields = ('name',)


@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'shift_template', 'status', 'start_time', 'end_time', 'total_revenue')
    list_filter = ('status',)
    search_fields = ('user__first_name', 'user__last_name')
    autocomplete_fields = ('user', 'shift_template')
    date_hierarchy = 'start_time'


@admin.register(CashReconciliation)
class CashReconciliationAdmin(admin.ModelAdmin):
    list_display = ('id', 'shift', 'expected_cash', 'actual_cash', 'difference', 'created_at')
    autocomplete_fields = ('shift', 'reconciled_by')
