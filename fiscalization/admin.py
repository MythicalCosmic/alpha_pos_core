from django.contrib import admin

from fiscalization.models import FiscalReceipt


@admin.register(FiscalReceipt)
class FiscalReceiptAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'order_id', 'receipt_type', 'status', 'provider', 'mode',
        'fiscal_sign', 'amount', 'attempts', 'fiscalized_at',
    )
    list_filter = ('status', 'receipt_type', 'provider', 'mode')
    search_fields = ('order__id', 'fiscal_sign', 'fiscal_number')
    readonly_fields = ('created_at', 'updated_at', 'fiscalized_at')
