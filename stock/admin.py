from django.contrib import admin
from .models import (
    StockLocation, StockUnit, StockCategory, StockItem, StockItemUnit,
    Recipe, RecipeIngredient, RecipeIngredientSubstitute, RecipeByProduct, RecipeStep,
    ProductStockLink, ProductComponentStock,
    Supplier, SupplierStockItem,
    PurchaseOrder, PurchaseOrderItem, PurchaseReceiving, PurchaseReceivingItem,
    StockLevel, StockBatch, StockTransaction,
    ProductionOrder, ProductionOrderIngredient, ProductionOrderOutput, ProductionOrderStep,
    StockTransfer, StockTransferItem,
    VarianceReasonCode, StockCount, StockCountItem,
    StockSettings, StockAlertConfig,
)


@admin.register(StockLocation)
class StockLocationAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'type', 'is_default', 'is_active', 'sort_order')
    list_filter = ('type', 'is_active', 'is_default')
    search_fields = ('name',)


@admin.register(StockUnit)
class StockUnitAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'short_name', 'unit_type', 'is_base_unit', 'conversion_factor')
    list_filter = ('unit_type', 'is_base_unit')
    search_fields = ('name', 'short_name')


@admin.register(StockCategory)
class StockCategoryAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name',)


@admin.register(StockItem)
class StockItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'sku', 'category', 'base_unit', 'is_active')
    list_filter = ('is_active', 'category')
    search_fields = ('name', 'sku')


@admin.register(StockItemUnit)
class StockItemUnitAdmin(admin.ModelAdmin):
    list_display = ('id', 'stock_item', 'unit', 'conversion_to_base', 'is_default')


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'difficulty_level', 'estimated_time_minutes')
    list_filter = ('difficulty_level',)
    search_fields = ('name',)


@admin.register(RecipeIngredient)
class RecipeIngredientAdmin(admin.ModelAdmin):
    list_display = ('id', 'recipe', 'stock_item', 'quantity', 'unit', 'is_optional')
    list_filter = ('is_optional',)


@admin.register(RecipeIngredientSubstitute)
class RecipeIngredientSubstituteAdmin(admin.ModelAdmin):
    list_display = ('id', 'recipe_ingredient', 'substitute_item', 'quantity', 'unit')


@admin.register(RecipeByProduct)
class RecipeByProductAdmin(admin.ModelAdmin):
    list_display = ('id', 'recipe', 'stock_item', 'expected_quantity', 'unit')


@admin.register(RecipeStep)
class RecipeStepAdmin(admin.ModelAdmin):
    list_display = ('id', 'recipe', 'step_number', 'title', 'duration_minutes')
    ordering = ('recipe', 'step_number')


@admin.register(ProductStockLink)
class ProductStockLinkAdmin(admin.ModelAdmin):
    list_display = ('id', 'product', 'link_type')
    list_filter = ('link_type',)


@admin.register(ProductComponentStock)
class ProductComponentStockAdmin(admin.ModelAdmin):
    list_display = ('id', 'product_stock_link', 'stock_item', 'quantity', 'unit')


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'contact_person', 'phone', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'contact_person', 'phone')


@admin.register(SupplierStockItem)
class SupplierStockItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'supplier', 'stock_item', 'price', 'is_preferred')
    list_filter = ('is_preferred',)


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'order_number', 'supplier', 'status', 'total', 'order_date')
    list_filter = ('status',)
    search_fields = ('order_number',)
    date_hierarchy = 'order_date'


@admin.register(PurchaseOrderItem)
class PurchaseOrderItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'purchase_order', 'stock_item', 'quantity_ordered', 'unit_price', 'total_price')


@admin.register(PurchaseReceiving)
class PurchaseReceivingAdmin(admin.ModelAdmin):
    list_display = ('id', 'purchase_order', 'status', 'received_date')
    list_filter = ('status',)


@admin.register(PurchaseReceivingItem)
class PurchaseReceivingItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'receiving', 'stock_item', 'quantity_received')


@admin.register(StockLevel)
class StockLevelAdmin(admin.ModelAdmin):
    list_display = ('id', 'stock_item', 'location', 'quantity', 'reserved_quantity')
    list_filter = ('location',)


@admin.register(StockBatch)
class StockBatchAdmin(admin.ModelAdmin):
    list_display = ('id', 'stock_item', 'location', 'batch_number', 'current_quantity', 'expiry_date')
    list_filter = ('location',)
    search_fields = ('batch_number',)


@admin.register(StockTransaction)
class StockTransactionAdmin(admin.ModelAdmin):
    # StockTransaction is an append-only ledger; the admin must not let
    # anyone edit or delete rows or the inventory audit trail breaks.
    list_display = ('id', 'stock_item', 'location', 'movement_type', 'quantity', 'created_at')
    list_filter = ('movement_type', 'location')
    date_hierarchy = 'created_at'

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # Allow viewing the change form (Django admin requires "change" perm
        # to render the detail page) but every field is readonly so nothing
        # can actually be modified.
        return True

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProductionOrder)
class ProductionOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'order_number', 'recipe', 'status', 'expected_output_qty', 'actual_output_qty')
    list_filter = ('status',)
    search_fields = ('order_number',)


@admin.register(ProductionOrderIngredient)
class ProductionOrderIngredientAdmin(admin.ModelAdmin):
    list_display = ('id', 'production_order', 'stock_item', 'planned_quantity', 'actual_quantity')


@admin.register(ProductionOrderOutput)
class ProductionOrderOutputAdmin(admin.ModelAdmin):
    list_display = ('id', 'production_order', 'stock_item', 'quantity')


@admin.register(ProductionOrderStep)
class ProductionOrderStepAdmin(admin.ModelAdmin):
    list_display = ('id', 'production_order', 'recipe_step', 'status')
    list_filter = ('status',)


@admin.register(StockTransfer)
class StockTransferAdmin(admin.ModelAdmin):
    list_display = ('id', 'transfer_number', 'from_location', 'to_location', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('transfer_number',)


@admin.register(StockTransferItem)
class StockTransferItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'transfer', 'stock_item', 'requested_qty', 'received_qty')


@admin.register(VarianceReasonCode)
class VarianceReasonCodeAdmin(admin.ModelAdmin):
    list_display = ('id', 'code', 'description', 'is_active')
    list_filter = ('is_active',)


@admin.register(StockCount)
class StockCountAdmin(admin.ModelAdmin):
    list_display = ('id', 'count_number', 'location', 'count_type', 'status', 'started_at')
    list_filter = ('status', 'count_type')
    search_fields = ('count_number',)


@admin.register(StockCountItem)
class StockCountItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'stock_count', 'stock_item', 'system_quantity', 'counted_quantity', 'variance')


@admin.register(StockSettings)
class StockSettingsAdmin(admin.ModelAdmin):
    list_display = ('id', 'stock_enabled', 'track_batches', 'auto_deduct_on_sale')


@admin.register(StockAlertConfig)
class StockAlertConfigAdmin(admin.ModelAdmin):
    list_display = ('id', 'alert_type', 'is_active')
    list_filter = ('alert_type', 'is_active')
