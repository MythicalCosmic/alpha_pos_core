from django.conf import settings
from base.services.sync.cache import safe_get, safe_set


CACHE_PREFIX = 'sync'

SYNC_ORDER = [
    # Base models (synced first - other models depend on these)
    'user', 'category', 'deliveryperson', 'place', 'table', 'product',
    'customer',  # before 'order' — Order.customer FK depends on it
    'order', 'orderitem', 'orderpayment', 'cashregister', 'inkassa',
    'shifttemplate', 'shift', 'orderrefund', 'cashreconciliation',
    # Stock models (synced after base, respecting FK dependencies)
    'stocklocation', 'stockunit', 'stockcategory', 'stockitem',
    'stockitemunit', 'supplier', 'supplierstockitem', 'suppliertransaction',
    'recipe', 'recipeingredient', 'recipeingredientsubstitute',
    'recipebyproduct', 'recipestep',
    'productstocklink', 'productcomponentstock',
    'purchaseorder', 'purchaseorderitem',
    'purchasereceiving', 'purchasereceivingitem',
    'stocklevel', 'stockbatch', 'stocktransaction',
    'productionorder', 'productionorderingredient',
    'productionorderoutput', 'productionorderstep',
    'stocktransfer', 'stocktransferitem',
    'variancereasoncode', 'stockcount', 'stockcountitem',
    'stocksettings', 'stockalertconfig',
    # HR models
    'department', 'employee', 'expensecategory', 'expense',
    'salarypayment', 'salarybonus', 'salarydeduction', 'cashtransaction',
    'employeecontract', 'contractdocument',
    'leavetype', 'leaverequest', 'leavebalance',
    'attendance',
    'employeedocument',
    'performancereview', 'performancegoal',
    'employmentevent',
    # Discount models
    'discounttype', 'discount', 'orderdiscount', 'discountusage',
    # Cashbox / shift settlement (last: FK to shift/user/supplier/category,
    # all of which sync earlier). Category before the expense that references it.
    'cashboxexpensecategory', 'shiftpaymenttotal', 'cashboxexpense',
    # One-way branch -> cloud audit trail. Kept last so actor and target rows
    # have already had a chance to land.
    'auditlog',
]

MODEL_MAP = {
    # Base models
    'user': 'base.User',
    'category': 'base.Category',
    'deliveryperson': 'base.DeliveryPerson',
    'customer': 'base.Customer',
    'product': 'base.Product',
    'order': 'base.Order',
    'orderitem': 'base.OrderItem',
    'orderpayment': 'base.OrderPayment',
    'orderrefund': 'base.OrderRefund',
    'cashregister': 'base.CashRegister',
    'inkassa': 'base.Inkassa',
    'place': 'base.Place',
    'table': 'base.Table',
    'shifttemplate': 'base.ShiftTemplate',
    'shift': 'base.Shift',
    'cashreconciliation': 'base.CashReconciliation',
    'auditlog': 'base.AuditLog',
    # Stock models
    'stocklocation': 'stock.StockLocation',
    'stockunit': 'stock.StockUnit',
    'stockcategory': 'stock.StockCategory',
    'stockitem': 'stock.StockItem',
    'stockitemunit': 'stock.StockItemUnit',
    'supplier': 'stock.Supplier',
    'supplierstockitem': 'stock.SupplierStockItem',
    'suppliertransaction': 'stock.SupplierTransaction',
    'recipe': 'stock.Recipe',
    'recipeingredient': 'stock.RecipeIngredient',
    'recipeingredientsubstitute': 'stock.RecipeIngredientSubstitute',
    'recipebyproduct': 'stock.RecipeByProduct',
    'recipestep': 'stock.RecipeStep',
    'productstocklink': 'stock.ProductStockLink',
    'productcomponentstock': 'stock.ProductComponentStock',
    'purchaseorder': 'stock.PurchaseOrder',
    'purchaseorderitem': 'stock.PurchaseOrderItem',
    'purchasereceiving': 'stock.PurchaseReceiving',
    'purchasereceivingitem': 'stock.PurchaseReceivingItem',
    'stocklevel': 'stock.StockLevel',
    'stockbatch': 'stock.StockBatch',
    'stocktransaction': 'stock.StockTransaction',
    'productionorder': 'stock.ProductionOrder',
    'productionorderingredient': 'stock.ProductionOrderIngredient',
    'productionorderoutput': 'stock.ProductionOrderOutput',
    'productionorderstep': 'stock.ProductionOrderStep',
    'stocktransfer': 'stock.StockTransfer',
    'stocktransferitem': 'stock.StockTransferItem',
    'variancereasoncode': 'stock.VarianceReasonCode',
    'stockcount': 'stock.StockCount',
    'stockcountitem': 'stock.StockCountItem',
    'stocksettings': 'stock.StockSettings',
    'stockalertconfig': 'stock.StockAlertConfig',
    # HR models
    'department': 'hr.Department',
    'employee': 'hr.Employee',
    'expensecategory': 'hr.ExpenseCategory',
    'expense': 'hr.Expense',
    'salarypayment': 'hr.SalaryPayment',
    'salarybonus': 'hr.SalaryBonus',
    'salarydeduction': 'hr.SalaryDeduction',
    'cashtransaction': 'hr.CashTransaction',
    'employeecontract': 'hr.EmployeeContract',
    'contractdocument': 'hr.ContractDocument',
    'leavetype': 'hr.LeaveType',
    'leaverequest': 'hr.LeaveRequest',
    'leavebalance': 'hr.LeaveBalance',
    'attendance': 'hr.Attendance',
    'employeedocument': 'hr.EmployeeDocument',
    'performancereview': 'hr.PerformanceReview',
    'performancegoal': 'hr.PerformanceGoal',
    'employmentevent': 'hr.EmploymentEvent',
    # Discount models
    'discounttype': 'discounts.DiscountType',
    'discount': 'discounts.Discount',
    'orderdiscount': 'discounts.OrderDiscount',
    'discountusage': 'discounts.DiscountUsage',
    # Cashbox / shift settlement
    'cashboxexpensecategory': 'cashbox.CashboxExpenseCategory',
    'shiftpaymenttotal': 'cashbox.ShiftPaymentTotal',
    'cashboxexpense': 'cashbox.CashboxExpense',
}

FK_UUID_MAPPINGS = {
    # Base FK mappings
    'user_uuid': ('base', 'User', 'user'),
    'cashier_uuid': ('base', 'User', 'cashier'),
    'delivery_person_uuid': ('base', 'DeliveryPerson', 'delivery_person'),
    'customer_uuid': ('base', 'Customer', 'customer'),
    'category_uuid': ('base', 'Category', 'category'),
    'order_uuid': ('base', 'Order', 'order'),
    'product_uuid': ('base', 'Product', 'product'),
    'parent_category_uuid': ('base', 'Category', 'parent'),
    'place_uuid': ('base', 'Place', 'place'),
    'table_uuid': ('base', 'Table', 'table'),
    'shift_template_uuid': ('base', 'ShiftTemplate', 'shift_template'),
    'shift_uuid': ('base', 'Shift', 'shift'),
    'reconciled_by_uuid': ('base', 'User', 'reconciled_by'),
    'actor_uuid': ('base', 'User', 'actor'),
    # Stock FK mappings
    'stock_item_uuid': ('stock', 'StockItem', 'stock_item'),
    'location_uuid': ('stock', 'StockLocation', 'location'),
    'supplier_uuid': ('stock', 'Supplier', 'supplier'),
    'recipe_uuid': ('stock', 'Recipe', 'recipe'),
    'batch_uuid': ('stock', 'StockBatch', 'batch'),
    'base_unit_uuid': ('stock', 'StockUnit', 'base_unit'),
    'unit_uuid': ('stock', 'StockUnit', 'unit'),
    'output_item_uuid': ('stock', 'StockItem', 'output_item'),
    'output_unit_uuid': ('stock', 'StockUnit', 'output_unit'),
    'parent_location_uuid': ('stock', 'StockLocation', 'parent_location'),
    'parent_uuid': ('stock', 'StockCategory', 'parent'),
    'production_order_uuid': ('stock', 'ProductionOrder', 'production_order'),
    'purchase_order_uuid': ('stock', 'PurchaseOrder', 'purchase_order'),
    'transfer_uuid': ('stock', 'StockTransfer', 'transfer'),
    'created_by_uuid': ('base', 'User', 'created_by'),
    'approved_by_uuid': ('base', 'User', 'approved_by'),
    'assigned_to_uuid': ('base', 'User', 'assigned_to'),
    'received_by_uuid': ('base', 'User', 'received_by'),
    'requested_by_uuid': ('base', 'User', 'requested_by'),
    'shipped_by_uuid': ('base', 'User', 'shipped_by'),
    'counted_by_uuid': ('base', 'User', 'counted_by'),
    'completed_by_uuid': ('base', 'User', 'completed_by'),
    # Stock-specific FK mappings
    'stock_category_uuid': ('stock', 'StockCategory', 'category'),
    'category_filter_uuid': ('stock', 'StockCategory', 'category_filter'),
    'production_location_uuid': ('stock', 'StockLocation', 'production_location'),
    'from_location_uuid': ('stock', 'StockLocation', 'from_location'),
    'to_location_uuid': ('stock', 'StockLocation', 'to_location'),
    'source_location_uuid': ('stock', 'StockLocation', 'source_location'),
    'output_location_uuid': ('stock', 'StockLocation', 'output_location'),
    'delivery_location_uuid': ('stock', 'StockLocation', 'delivery_location'),
    'default_location_uuid': ('stock', 'StockLocation', 'default_location'),
    'default_production_location_uuid': ('stock', 'StockLocation', 'default_production_location'),
    'default_receiving_location_uuid': ('stock', 'StockLocation', 'default_receiving_location'),
    'parent_recipe_uuid': ('stock', 'Recipe', 'parent_recipe'),
    'recipe_ingredient_uuid': ('stock', 'RecipeIngredient', 'recipe_ingredient'),
    'recipe_step_uuid': ('stock', 'RecipeStep', 'recipe_step'),
    'substitute_item_uuid': ('stock', 'StockItem', 'substitute_item'),
    'product_stock_link_uuid': ('stock', 'ProductStockLink', 'product_stock_link'),
    'supplier_stock_item_uuid': ('stock', 'SupplierStockItem', 'supplier_stock_item'),
    'po_item_uuid': ('stock', 'PurchaseOrderItem', 'po_item'),
    'receiving_uuid': ('stock', 'PurchaseReceiving', 'receiving'),
    'batch_created_uuid': ('stock', 'StockBatch', 'batch_created'),
    'batch_used_uuid': ('stock', 'StockBatch', 'batch_used'),
    'stock_count_uuid': ('stock', 'StockCount', 'stock_count'),
    'reason_code_uuid': ('stock', 'VarianceReasonCode', 'reason_code'),
    'adjustment_transaction_uuid': ('stock', 'StockTransaction', 'adjustment_transaction'),
    # HR FK mappings
    'manager_uuid': ('base', 'User', 'manager'),
    'department_uuid': ('hr', 'Department', 'department'),
    'employee_uuid': ('hr', 'Employee', 'employee'),
    'expense_category_uuid': ('hr', 'ExpenseCategory', 'category'),
    'paid_by_uuid': ('base', 'User', 'paid_by'),
    'performed_by_uuid': ('base', 'User', 'performed_by'),
    # HR expansion FK mappings
    'renewed_from_uuid': ('hr', 'EmployeeContract', 'renewed_from'),
    'contract_uuid': ('hr', 'EmployeeContract', 'contract'),
    'leave_type_uuid': ('hr', 'LeaveType', 'leave_type'),
    'salary_uuid': ('hr', 'SalaryPayment', 'salary'),
    'reviewer_uuid': ('base', 'User', 'reviewer'),
    'verified_by_uuid': ('base', 'User', 'verified_by'),
    'uploaded_by_uuid': ('base', 'User', 'uploaded_by'),
    # Discount FK mappings
    'discount_type_uuid': ('discounts', 'DiscountType', 'discount_type'),
    'discount_uuid': ('discounts', 'Discount', 'discount'),
    'free_product_uuid': ('base', 'Product', 'free_product'),
    'applied_by_uuid': ('base', 'User', 'applied_by'),
    # Cashbox FK mappings (distinct uuid keys — category_uuid is claimed by
    # base.Category, so the cashbox category uses its own key).
    'cashbox_category_uuid': ('cashbox', 'CashboxExpenseCategory', 'category'),
    'recipient_user_uuid': ('base', 'User', 'recipient_user'),
    'recipient_supplier_uuid': ('stock', 'Supplier', 'recipient_supplier'),
}


def get_branch_id():
    return getattr(settings, 'BRANCH_ID', '')


def get_device_id():
    """Per-install till identity (minted at desktop install). Distinguishes
    multiple tills that may share one branch token. Empty on the cloud."""
    return getattr(settings, 'DEVICE_ID', '')


def get_deployment_mode():
    return getattr(settings, 'DEPLOYMENT_MODE', 'local')


def get_cloud_url():
    return getattr(settings, 'CLOUD_SYNC_URL', '').rstrip('/')


def get_cloud_token():
    return getattr(settings, 'CLOUD_SYNC_TOKEN', '')


def get_sync_interval():
    return getattr(settings, 'SYNC_INTERVAL', 30)


def get_sync_retry_interval():
    return getattr(settings, 'SYNC_RETRY_INTERVAL', 60)


def get_sync_timeout():
    return getattr(settings, 'SYNC_TIMEOUT', 30)


def get_sync_max_retries():
    return getattr(settings, 'SYNC_MAX_RETRIES', 5)


def get_sync_batch_size():
    return getattr(settings, 'SYNC_BATCH_SIZE', 500)


def get_sync_max_queue_attempts():
    # After this many failed delivery attempts a queued record is treated as a
    # poison message: it stops being re-sent every cycle (dead-lettered) so a
    # permanently-rejected row can't spin forever or drown out healthy records.
    # 0 disables the cap. Surfaced via SyncQueue.dead_letter_count().
    return getattr(settings, 'SYNC_MAX_QUEUE_ATTEMPTS', 25)


def get_pull_enabled():
    return getattr(settings, 'SYNC_PULL_ENABLED', True)


def get_sync_require_https():
    # When True, refuse to talk to a plaintext http:// cloud URL (the branch
    # token and password hashes would traverse it in clear). Off by default so
    # existing LAN deployments keep working, but logged as a warning.
    return getattr(settings, 'SYNC_REQUIRE_HTTPS', False)


def is_local_mode():
    return get_deployment_mode() == 'local'


def resolve_model(name):
    from django.apps import apps
    dotted = MODEL_MAP.get(name.lower())
    if not dotted:
        return None
    app_label, model_name = dotted.split('.')
    return apps.get_model(app_label, model_name)


def get_all_models():
    return {name: resolve_model(name) for name in SYNC_ORDER}


class SyncConfig:

    @classmethod
    def _key(cls, part):
        return f'{CACHE_PREFIX}:config:{part}'

    @classmethod
    def is_enabled(cls):
        override = safe_get(cls._key('enabled'))
        if override is not None:
            return override
        return getattr(settings, 'SYNC_ENABLED', False)

    @classmethod
    def enable(cls):
        safe_set(cls._key('enabled'), True, None)

    @classmethod
    def disable(cls):
        safe_set(cls._key('enabled'), False, None)

    @classmethod
    def get_status(cls):
        return {
            'enabled': cls.is_enabled(),
            'mode': get_deployment_mode(),
            'branch_id': get_branch_id(),
            'cloud_url': get_cloud_url(),
            'interval': get_sync_interval(),
            'batch_size': get_sync_batch_size(),
            'max_retries': get_sync_max_retries(),
            'pull_enabled': get_pull_enabled(),
        }
