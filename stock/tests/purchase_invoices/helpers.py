from base.models import AuditLog, IdempotencyKey, TreasuryTransaction
from hr.models import Expense
from stock.models import (
    PurchaseOrder,
    PurchaseReceiving,
    PurchaseReceivingItem,
    StockBatch,
    StockTransaction,
    SupplierTransaction,
)
from stock.services.purchase_invoices import json_numbers

BASE = "/api/admins/stock/purchase-invoices/"


def post(data, payload=None, key="invoice-post-1", client=None):
    return (client or data.client).post(
        BASE + "receive/",
        json_numbers.dumps(payload or data.payload),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=key,
    )


def ledger_state(data):
    for obj in (data.item, data.level, data.supplier, data.link):
        obj.refresh_from_db()
    return {
        "quantity": data.level.quantity,
        "average": data.item.avg_cost_price,
        "last": data.item.last_cost_price,
        "manual": data.item.cost_price,
        "price": data.link.price,
        "known": data.link.price_is_known,
        "balance": data.supplier.current_balance,
        "po": PurchaseOrder.objects.count(),
        "receivings": PurchaseReceiving.objects.count(),
        "receiving_lines": PurchaseReceivingItem.objects.count(),
        "batches": StockBatch.objects.count(),
        "stock_transactions": StockTransaction.objects.count(),
        "supplier_transactions": SupplierTransaction.objects.count(),
        "audit": AuditLog.objects.count(),
        "idempotency": IdempotencyKey.objects.count(),
        "expenses": Expense.objects.count(),
        "treasury": TreasuryTransaction.objects.count(),
    }
