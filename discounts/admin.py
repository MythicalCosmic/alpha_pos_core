from django.contrib import admin
from .models import DiscountType, Discount, OrderDiscount, DiscountUsage


@admin.register(DiscountType)
class DiscountTypeAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'code', 'discount_method', 'is_active')
    list_filter = ('discount_method', 'is_active')
    search_fields = ('name', 'code')


@admin.register(Discount)
class DiscountAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'code', 'discount_type', 'value', 'is_active', 'start_date', 'end_date')
    list_filter = ('is_active', 'discount_type', 'applies_to')
    search_fields = ('name', 'code')
    autocomplete_fields = ('discount_type', 'free_product', 'created_by')


@admin.register(OrderDiscount)
class OrderDiscountAdmin(admin.ModelAdmin):
    list_display = ('id', 'order', 'discount', 'discount_code', 'discount_amount', 'created_at')
    autocomplete_fields = ('order', 'discount', 'applied_by')


@admin.register(DiscountUsage)
class DiscountUsageAdmin(admin.ModelAdmin):
    list_display = ('id', 'discount', 'user', 'order', 'used_at')
    autocomplete_fields = ('discount', 'user', 'order')
