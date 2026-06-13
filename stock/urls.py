from django.urls import path
from stock.views import (
    settings_views, location_views, unit_views, category_views,
    item_views, level_views, batch_views, supplier_views,
    purchase_views, recipe_views, production_views,
    transfer_views, count_views, product_link_views,
    order_views, ai_views,
)

app_name = 'stock'

urlpatterns = [
    # Settings & Alerts
    path('settings/', settings_views.settings, name='settings'),
    path('settings/toggle/', settings_views.settings_toggle, name='settings-toggle'),
    path('alerts/', settings_views.alerts, name='alerts'),

    # Locations
    path('locations/', location_views.locations, name='location-list'),
    path('locations/<int:location_id>/', location_views.location_detail, name='location-detail'),
    path('locations/<int:location_id>/set-default/', location_views.location_set_default, name='location-set-default'),

    # Units
    path('units/', unit_views.units, name='unit-list'),
    path('units/<int:unit_id>/', unit_views.unit_detail, name='unit-detail'),
    path('units/convert/', unit_views.unit_convert, name='unit-convert'),

    # Categories
    path('categories/', category_views.categories, name='category-list'),
    path('categories/<int:category_id>/', category_views.category_detail, name='category-detail'),

    # Stock Items
    path('items/', item_views.stock_items, name='item-list'),
    path('items/search/', item_views.stock_item_search, name='item-search'),
    path('items/stats/', item_views.stock_item_stats, name='item-stats'),
    path('items/barcode/<str:barcode>/', item_views.stock_item_barcode, name='item-barcode'),
    path('items/<int:item_id>/', item_views.stock_item_detail, name='item-detail'),

    # Stock Levels
    path('levels/', level_views.stock_levels, name='level-list'),
    path('levels/item/<int:item_id>/', level_views.stock_level_item, name='level-item'),
    path('levels/location/<int:location_id>/', level_views.stock_level_location, name='level-location'),
    path('low-stock/', level_views.low_stock, name='low-stock'),

    # Stock Adjustments & Reservations
    path('adjust/', level_views.stock_adjust, name='adjust'),
    path('reserve/', level_views.stock_reserve, name='reserve'),
    path('release-reservation/', level_views.stock_release_reservation, name='release-reservation'),

    # Transactions
    path('transactions/', level_views.transactions, name='transaction-list'),
    path('transactions/item/<int:item_id>/', level_views.transaction_history, name='transaction-history'),

    # Batches
    path('batches/', batch_views.batches, name='batch-list'),
    path('batches/expiring/', batch_views.expiring_batches, name='batch-expiring'),
    path('batches/expired/', batch_views.expired_batches, name='batch-expired'),
    path('batches/auto-consume/', batch_views.batch_auto_consume, name='batch-auto-consume'),
    path('batches/<int:batch_id>/', batch_views.batch_detail, name='batch-detail'),
    path('batches/<int:batch_id>/consume/', batch_views.batch_consume, name='batch-consume'),

    # Suppliers
    path('suppliers/', supplier_views.suppliers, name='supplier-list'),
    path('suppliers/<int:supplier_id>/', supplier_views.supplier_detail, name='supplier-detail'),
    path('suppliers/<int:supplier_id>/items/', supplier_views.supplier_items, name='supplier-items'),
    path('suppliers/<int:supplier_id>/pay/', supplier_views.supplier_pay, name='supplier-pay'),
    path('suppliers/<int:supplier_id>/ledger/', supplier_views.supplier_ledger, name='supplier-ledger'),

    # Purchase Orders
    path('purchase-orders/', purchase_views.purchase_orders, name='po-list'),
    path('purchase-orders/<int:po_id>/', purchase_views.purchase_order_detail, name='po-detail'),
    path('purchase-orders/<int:po_id>/items/', purchase_views.purchase_order_items, name='po-items'),
    path('purchase-orders/<int:po_id>/<str:action>/', purchase_views.purchase_order_action, name='po-action'),
    path('purchase-order/<int:po_id>/receiving/', purchase_views.purchase_receiving, name='po-receiving'),
    path('purchase-order-items/<int:item_id>/', purchase_views.purchase_order_item_detail, name='po-item-detail'),
    path('receiving/<int:receiving_id>/items/', purchase_views.purchase_receiving_items, name='receiving-items'),
    path('receiving/<int:receiving_id>/complete/', purchase_views.purchase_receiving_complete, name='receiving-complete'),

    # Recipes
    path('recipes/', recipe_views.recipes, name='recipe-list'),
    path('recipes/<int:recipe_id>/', recipe_views.recipe_detail, name='recipe-detail'),
    path('recipes/<int:recipe_id>/cost/', recipe_views.recipe_cost, name='recipe-cost'),
    path('recipes/<int:recipe_id>/availability/', recipe_views.recipe_availability, name='recipe-availability'),
    path('recipes/<int:recipe_id>/ingredients/', recipe_views.recipe_ingredients, name='recipe-ingredients'),
    path('recipe-ingredients/<int:ingredient_id>/', recipe_views.recipe_ingredient_detail, name='recipe-ingredient-detail'),

    # Production Orders
    path('production-orders/', production_views.production_orders, name='production-list'),
    path('production-orders/<int:order_id>/', production_views.production_order_detail, name='production-detail'),
    path('production-orders/<int:order_id>/<str:action>/', production_views.production_order_action, name='production-action'),

    # Transfers
    path('transfers/', transfer_views.transfers, name='transfer-list'),
    path('transfers/quick/', transfer_views.quick_transfer, name='transfer-quick'),
    path('transfers/<int:transfer_id>/', transfer_views.transfer_detail, name='transfer-detail'),
    path('transfers/<int:transfer_id>/items/', transfer_views.transfer_items, name='transfer-items'),
    path('transfers/<int:transfer_id>/<str:action>/', transfer_views.transfer_action, name='transfer-action'),

    # Stock Counts
    path('counts/', count_views.stock_counts, name='count-list'),
    path('counts/<int:count_id>/', count_views.stock_count_detail, name='count-detail'),
    path('counts/<int:count_id>/record/', count_views.stock_count_record, name='count-record'),
    path('counts/<int:count_id>/<str:action>/', count_views.stock_count_action, name='count-action'),
    path('variance-codes/', count_views.variance_codes, name='variance-codes'),
    path('variance-codes/seed/', count_views.variance_codes_seed, name='variance-codes-seed'),

    # Product Links
    path('product-links/', product_link_views.product_links, name='product-link-list'),
    path('product-links/<int:link_id>/', product_link_views.product_link_detail, name='product-link-detail'),
    path('products/<int:product_id>/link/', product_link_views.product_link_by_product, name='product-link-get'),
    path('products/<int:product_id>/link-recipe/', product_link_views.product_link_to_recipe, name='product-link-recipe'),
    path('products/<int:product_id>/link-item/', product_link_views.product_link_to_item, name='product-link-item'),
    path('products/<int:product_id>/link-components/', product_link_views.product_link_with_components, name='product-link-components'),
    path('products/<int:product_id>/unlink/', product_link_views.product_unlink, name='product-unlink'),

    # Order Stock Integration
    path('orders/deduct/', order_views.order_stock_deduct, name='order-deduct'),
    path('orders/reverse/', order_views.order_stock_reverse, name='order-reverse'),
    path('orders/check-availability/', order_views.order_stock_availability, name='order-check-availability'),
    path('orders/reserve/', order_views.order_stock_reserve, name='order-reserve'),

    # AI Assistant
    path('ai/query/', ai_views.ai_query, name='ai-query'),
    path('ai/suggestions/', ai_views.ai_suggestions, name='ai-suggestions'),
    path('ai/quick-actions/', ai_views.ai_quick_actions, name='ai-quick-actions'),
]
