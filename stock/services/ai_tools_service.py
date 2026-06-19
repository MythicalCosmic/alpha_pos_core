"""Read-only data tools for the Claude-powered AI assistant.

The old assistant could only see a fixed, pre-aggregated snapshot (top-15 products,
first 50 stock rows, today/week/month rollups) stuffed into one prompt — so it could
never answer "what's inside order #42", "who's on shift right now", or "what did Ali
sell on 2026-06-10". You cannot fit every order with its line items into a single
context window, so instead we give Claude a set of read-only tools and let it drill
into exactly what the question needs, on demand.

`AIToolbox.TOOLS` is the Anthropic tool schema list; `AIToolbox.execute(name, args)`
runs one tool against the ORM and returns a JSON string. Everything is strictly
read-only (no writes, no deletes) and excludes soft-deleted rows. Sensitive fields
(password hashes, raw session payloads) are never serialized.
"""
from datetime import date, timedelta
import json
import logging

from django.db.models import Sum, Count, F, Q, Avg
from django.db.models.functions import TruncDate, TruncHour
from django.utils import timezone

from base.models import (
    User, Order, OrderItem, Product, Category, Customer,
    CashRegister, Inkassa, Shift, CashReconciliation,
)
from stock.models import StockLevel, StockBatch, StockLocation

logger = logging.getLogger(__name__)

# Per-call result caps. Tool results are fed back into the model's context, so an
# unbounded list could blow the window; the model can always page with offset or
# narrow with filters. These are generous ("super detail") but finite.
MAX_ORDERS = 200
MAX_LIST = 300
MAX_SHIFT_ORDERS = 300
# A single analytics block (abc/menu/etc.) can list the whole catalog; cap the
# detail rows fed back into the model so one tool result can't blow the context.
ANALYTICS_ITEM_CAP = 100
_OFFSET_MAX = 10_000_000


def _clamp(value, default, lo, hi):
    """Coerce a model-supplied limit/offset to a sane int in [lo, hi]; a bad or
    negative value falls back to `default` (negatives would make odd slices)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


def _f(v):
    """Decimal/None -> float for clean JSON (Decimals don't serialize natively)."""
    return float(v) if v is not None else 0.0


def _name(u):
    if not u:
        return None
    return f"{u.first_name} {u.last_name}".strip() or u.email or f"User #{u.id}"


def _iso(dt):
    return dt.isoformat() if dt else None


def _parse_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def _customer_brief(c):
    if not c:
        return None
    return {
        "id": c.id,
        "name": c.name or None,
        "phone": c.phone_number or None,
        "is_staff": c.is_staff,
        "telegram_id": c.telegram_id,
    }


def _order_summary(o):
    """Compact order row for list views — enough to recognize and pick an order,
    with a short item preview, without serializing every line."""
    items = [it for it in o.items.all() if not it.is_deleted]
    preview = [f"{it.quantity} x {it.product.name if it.product else '?'}" for it in items[:8]]
    due = _f(o.total_amount) * (1 - _f(o.discount_percent) / 100)
    return {
        "id": o.id,
        "display_id": o.display_id,
        "created_at": _iso(o.created_at),
        "status": o.status,
        "order_type": o.order_type,
        "is_paid": o.is_paid,
        "payment_method": o.payment_method,
        "subtotal_uzs": _f(o.subtotal),
        "discount_amount_uzs": _f(o.discount_amount),
        "discount_percent": _f(o.discount_percent),
        "total_amount_uzs": _f(o.total_amount),
        "amount_due_uzs": round(due, 2),
        "cashier": _name(o.cashier),
        "cashier_id": o.cashier_id,
        "customer": _customer_brief(o.customer),
        "table": o.table.number if o.table else None,
        "place": o.place.name if o.place else None,
        "item_count": len(items),
        "items_preview": preview,
    }


def _order_full(o):
    """Everything about one order, including every line item and every payment —
    this is the 'what is inside the order, super detail' view."""
    data = _order_summary(o)
    items = [it for it in o.items.all() if not it.is_deleted]
    data["items"] = [{
        "product": it.product.name if it.product else None,
        "product_id": it.product_id,
        "category": (it.product.category.name
                     if it.product and it.product.category_id else None),
        "quantity": it.quantity,
        "unit_price_uzs": _f(it.price),
        "original_price_uzs": _f(it.original_price),
        "line_discount_uzs": _f(it.discount_amount),
        "line_total_uzs": _f(it.price) * it.quantity,
        "note": it.detail or None,
        "ready_at": _iso(it.ready_at),
    } for it in items]
    payments = [p for p in o.payments.all() if not p.is_deleted]
    data["payments"] = [{
        "method": p.method,
        "amount_uzs": _f(p.amount),
        "at": _iso(p.created_at),
    } for p in payments]
    data["delivery_person"] = _name(o.delivery_person) if o.delivery_person_id else None
    data["phone_number"] = o.phone_number or None
    data["description"] = o.description or None
    data["chef_queue_number"] = o.chef_queue_number
    data["ready_at"] = _iso(o.ready_at)
    data["paid_at"] = _iso(o.paid_at)
    data["updated_at"] = _iso(o.updated_at)
    return data


def _shift_orders_qs(shift):
    """Orders attributed to a shift. There is no Order->Shift FK; attribution is
    by cashier + the shift's time window (open shifts run up to 'now')."""
    end = shift.end_time or timezone.now()
    return Order.objects.filter(
        is_deleted=False, cashier=shift.user,
        created_at__gte=shift.start_time, created_at__lt=end,
    )


def _shift_dict(s, with_orders=False):
    oqs = _shift_orders_qs(s)
    agg = oqs.aggregate(
        cnt=Count("id"),
        revenue=Sum("total_amount", filter=Q(is_paid=True)),
        gross=Sum("total_amount"),
    )
    # Reverse one-to-one: accessing a missing .reconciliation raises DoesNotExist
    # (not AttributeError), so getattr(..., None) would not catch it.
    try:
        recon = s.reconciliation
    except CashReconciliation.DoesNotExist:
        recon = None
    data = {
        "id": s.id,
        "cashier": _name(s.user),
        "cashier_id": s.user_id,
        "status": s.status,
        "is_open": s.status == Shift.Status.ACTIVE,
        "start_time": _iso(s.start_time),
        "end_time": _iso(s.end_time),
        "shift_template": s.shift_template.name if s.shift_template_id else None,
        "notes": s.notes or None,
        # Live figures computed from orders in the window (the stored counters on
        # the Shift row are only frozen at close, so they can lag for open shifts).
        "live_orders": agg["cnt"] or 0,
        "live_paid_revenue_uzs": _f(agg["revenue"]),
        "live_gross_uzs": _f(agg["gross"]),
        # Stored counters (authoritative once the shift is ENDED/COMPLETED).
        "stored_total_orders": s.total_orders,
        "stored_total_revenue_uzs": _f(s.total_revenue),
        "stored_cash_collected_uzs": _f(s.cash_collected),
    }
    if recon and not recon.is_deleted:
        data["reconciliation"] = {
            "expected_cash_uzs": _f(recon.expected_cash),
            "actual_cash_uzs": _f(recon.actual_cash),
            "difference_uzs": _f(recon.difference),
            "reconciled_by": _name(recon.reconciled_by) if recon.reconciled_by_id else None,
            "at": _iso(recon.created_at),
            "notes": recon.notes or None,
        }
    if with_orders:
        orders = (oqs.select_related("cashier", "customer", "table", "place")
                  .prefetch_related("items__product").order_by("-created_at")[:MAX_SHIFT_ORDERS])
        data["orders"] = [_order_summary(o) for o in orders]
    return data


def _product_dict(p):
    return {
        "id": p.id,
        "name": p.name,
        "category": p.category.name if p.category_id else None,
        "price_uzs": _f(p.price),
        "description": p.description or None,
        "is_instant": p.is_instant,
        "ikpu_code": p.ikpu_code or None,
        "created_at": _iso(p.created_at),
    }


def _stock_dict(level):
    item = level.stock_item
    qty = _f(level.quantity)
    reserved = _f(level.reserved_quantity)
    avg_cost = _f(item.avg_cost_price)
    reorder = _f(item.reorder_point)
    return {
        "item": item.name,
        "sku": item.sku,
        "barcode": item.barcode,
        "item_type": item.item_type,
        "location": level.location.name if level.location_id else None,
        "quantity": qty,
        "reserved": reserved,
        "available": qty - reserved,
        "unit": item.base_unit.short_name if item.base_unit_id else None,
        "reorder_point": reorder,
        "min_level": _f(item.min_stock_level),
        "max_level": _f(item.max_stock_level),
        "avg_cost_uzs": avg_cost,
        "value_uzs": round(qty * avg_cost, 2),
        "is_low": qty <= reorder,
        "is_out": qty <= 0,
        "is_active": item.is_active,
        "track_expiry": item.track_expiry,
        "last_movement_at": _iso(level.last_movement_at),
    }


def _cashier_dict(u, with_today=True):
    open_shift = (Shift.objects.filter(is_deleted=False, user=u, status=Shift.Status.ACTIVE)
                  .order_by("-start_time").first())
    data = {
        "id": u.id,
        "name": _name(u),
        "role": u.role,
        "status": u.status,
        "email": u.email or None,
        "last_login_at": _iso(u.last_login_at),
        "on_shift": open_shift is not None,
        "open_shift_since": _iso(open_shift.start_time) if open_shift else None,
        "open_shift_id": open_shift.id if open_shift else None,
    }
    if with_today:
        # "Today" is a wall-clock concept: filter on the local calendar date
        # (TIME_ZONE is Asia/Tashkent, UTC+5) so 00:00-05:00 local orders count.
        agg = Order.objects.filter(
            is_deleted=False, cashier=u, created_at__date=timezone.localdate()
        ).aggregate(cnt=Count("id"), rev=Sum("total_amount", filter=Q(is_paid=True)))
        data["today_orders"] = agg["cnt"] or 0
        data["today_revenue_uzs"] = _f(agg["rev"])
    return data


def _cap_analytics(block):
    """Bound the detail lists inside an analytics block — a single block (ABC,
    menu, profitability...) can enumerate the whole catalog, and that whole dict
    is fed back to the model as one tool result. Trim to ANALYTICS_ITEM_CAP rows
    and record the original length; the summary/count fields stay intact."""
    if isinstance(block, dict):
        for key in ("items", "products"):
            lst = block.get(key)
            if isinstance(lst, list) and len(lst) > ANALYTICS_ITEM_CAP:
                block[key] = lst[:ANALYTICS_ITEM_CAP]
                block.setdefault("_truncated", {})[key] = len(lst)
    return block


class AIToolbox:
    """Read-only tool catalog + dispatcher for the AI assistant."""

    TOOLS = [
        {
            "name": "get_datetime",
            "description": "Get the current server date and time. Call this first whenever the user asks about 'today', 'now', 'this week', 'yesterday' or any relative date, so you anchor relative dates correctly.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_overview",
            "description": "A quick business snapshot: today's sales totals, open shifts, cash balance, stock health summary, and record counts. Use it to orient yourself, then call the detailed tools for specifics.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_orders",
            "description": "List orders with filters. Returns a compact row per order (totals, cashier, customer, an item preview and item_count) plus total_matching for paging. Use get_order for the full line-item breakdown of a specific order.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Single day YYYY-MM-DD"},
                    "date_from": {"type": "string", "description": "Range start YYYY-MM-DD (inclusive)"},
                    "date_to": {"type": "string", "description": "Range end YYYY-MM-DD (inclusive)"},
                    "status": {"type": "string", "enum": ["OPEN", "PREPARING", "READY", "COMPLETED", "CANCELED"]},
                    "order_type": {"type": "string", "enum": ["HALL", "DELIVERY", "PICKUP"]},
                    "payment_method": {"type": "string", "enum": ["CASH", "UZCARD", "HUMO", "PAYME", "MIXED"]},
                    "is_paid": {"type": "boolean"},
                    "cashier_id": {"type": "integer", "description": "Filter by cashier user id"},
                    "customer_phone": {"type": "string", "description": "Match order or customer phone (partial ok)"},
                    "product_name": {"type": "string", "description": "Only orders containing a product whose name matches (partial)"},
                    "limit": {"type": "integer", "description": f"Max rows (default 50, max {MAX_ORDERS})"},
                    "offset": {"type": "integer", "description": "Skip N rows for paging"},
                },
            },
        },
        {
            "name": "get_order",
            "description": "Full detail of ONE order: every line item (product, quantity, unit price, discounts, notes), every payment line (for split payments), customer, cashier, table/place, and all timestamps. This is how you see exactly what is inside an order.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "integer", "description": "Database id of the order (preferred, unambiguous)"},
                    "display_id": {"type": "integer", "description": "Receipt/screen number (per-branch, wraps at 100 — pass 'date' to disambiguate)"},
                    "date": {"type": "string", "description": "YYYY-MM-DD to disambiguate a display_id"},
                },
            },
        },
        {
            "name": "get_open_shifts",
            "description": "Every shift that is currently OPEN (cashier signed in, not yet closed), with live order count and revenue computed from this shift's orders so far. Use for 'who is working now' / 'open shifts'.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_shifts",
            "description": "List shifts (open and closed) with filters. Each row has live and stored totals plus cash reconciliation if any.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["ACTIVE", "ENDED", "COMPLETED", "ABANDONED"]},
                    "cashier_id": {"type": "integer"},
                    "date": {"type": "string", "description": "Shifts that started on this day YYYY-MM-DD"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "limit": {"type": "integer", "description": "Default 50"},
                    "offset": {"type": "integer", "description": "Skip N rows for paging"},
                },
            },
        },
        {
            "name": "get_shift",
            "description": "Full detail of one shift including the list of orders rung up during it, totals, and cash reconciliation.",
            "input_schema": {
                "type": "object",
                "properties": {"shift_id": {"type": "integer"}},
                "required": ["shift_id"],
            },
        },
        {
            "name": "list_cashiers",
            "description": "List staff/cashiers with role, status, whether they are on shift right now, and today's order count and revenue. Use to answer 'every cashier' questions or to find a cashier_id.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "enum": ["CASHIER", "MANAGER", "ADMIN", "WAITER", "CHEF", "USER"]},
                    "only_on_shift": {"type": "boolean"},
                    "limit": {"type": "integer", "description": f"Default {MAX_LIST}"},
                    "offset": {"type": "integer", "description": "Skip N rows for paging"},
                },
            },
        },
        {
            "name": "get_cashier",
            "description": "Detail of one cashier/user: recent shifts, last 30-day performance, and most recent orders.",
            "input_schema": {
                "type": "object",
                "properties": {"cashier_id": {"type": "integer"}},
                "required": ["cashier_id"],
            },
        },
        {
            "name": "list_products",
            "description": "List the product catalog (menu) with price, category and flags. Filter by name or category.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Match product name (partial)"},
                    "category": {"type": "string", "description": "Match category name (partial)"},
                    "limit": {"type": "integer", "description": f"Default {MAX_LIST}"},
                    "offset": {"type": "integer", "description": "Skip N rows to page the full catalog"},
                },
            },
        },
        {
            "name": "list_stock",
            "description": "List stock/inventory levels per item per location: quantity, available, reorder point, value, low/out flags. Filter by item name, location, or only low/out items.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Match item name (partial)"},
                    "location": {"type": "string", "description": "Match location name (partial)"},
                    "low_only": {"type": "boolean", "description": "Only items at or below reorder point"},
                    "out_only": {"type": "boolean", "description": "Only items at or below zero"},
                    "include_inactive": {"type": "boolean", "description": "Also include levels of deactivated items (default false = active only)"},
                    "limit": {"type": "integer", "description": f"Default {MAX_LIST}"},
                    "offset": {"type": "integer", "description": "Skip N rows to page all stock"},
                },
            },
        },
        {
            "name": "sales_report",
            "description": "Aggregated sales for any date or date range: totals, per-day, per-cashier, per-category, top products, per payment method, per order type (and hourly for a single day). Use for revenue/trend questions over arbitrary dates.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Single day YYYY-MM-DD"},
                    "date_from": {"type": "string", "description": "Range start YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "Range end YYYY-MM-DD"},
                },
            },
        },
        {
            "name": "business_analytics",
            "description": "Advanced precomputed analytics. kind: abc, xyz, abc_xyz, menu (menu engineering), profitability, inventory_health, sales_velocity, or all.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["abc", "xyz", "abc_xyz", "menu", "profitability", "inventory_health", "sales_velocity", "all"]},
                    "days": {"type": "integer", "description": "Lookback window in days (default 30)"},
                },
                "required": ["kind"],
            },
        },
    ]

    # ── dispatcher ──
    @classmethod
    def execute(cls, name, args, location_id=None):
        args = args or {}
        handler = {
            "get_datetime": cls._t_datetime,
            "get_overview": cls._t_overview,
            "list_orders": cls._t_list_orders,
            "get_order": cls._t_get_order,
            "get_open_shifts": cls._t_open_shifts,
            "list_shifts": cls._t_list_shifts,
            "get_shift": cls._t_get_shift,
            "list_cashiers": cls._t_list_cashiers,
            "get_cashier": cls._t_get_cashier,
            "list_products": cls._t_list_products,
            "list_stock": cls._t_list_stock,
            "sales_report": cls._t_sales_report,
            "business_analytics": cls._t_analytics,
        }.get(name)
        if handler is None:
            return json.dumps({"error": f"unknown tool: {name}"})
        try:
            return json.dumps(handler(args), default=str, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001 — never crash the loop; report to the model
            logger.exception("AI tool %s failed", name)
            return json.dumps({"error": str(e)})

    # ── handlers ──
    @classmethod
    def _t_datetime(cls, args):
        local = timezone.localtime()  # in TIME_ZONE (Asia/Tashkent) for wall-clock answers
        return {
            "now": _iso(local),
            "today": timezone.localdate().isoformat(),
            "weekday": local.strftime("%A"),
            "timezone": str(timezone.get_current_timezone()),
        }

    @classmethod
    def _t_overview(cls, args):
        now = timezone.now()
        today_local = timezone.localdate()  # wall-clock day (Asia/Tashkent, UTC+5)
        today = Order.objects.filter(is_deleted=False, created_at__date=today_local)
        t_agg = today.aggregate(
            cnt=Count("id"),
            revenue=Sum("total_amount", filter=Q(is_paid=True)),
            paid=Count("id", filter=Q(is_paid=True)),
            unpaid=Count("id", filter=Q(is_paid=False)),
        )
        open_shifts = (Shift.objects.filter(is_deleted=False, status=Shift.Status.ACTIVE)
                       .select_related("user").order_by("-start_time"))
        open_list = []
        for s in open_shifts[:50]:
            a = _shift_orders_qs(s).aggregate(c=Count("id"), r=Sum("total_amount", filter=Q(is_paid=True)))
            open_list.append({
                "cashier": _name(s.user), "cashier_id": s.user_id,
                "since": _iso(s.start_time), "shift_id": s.id,
                "live_orders": a["c"] or 0, "live_revenue_uzs": _f(a["r"]),
            })

        levels = StockLevel.objects.filter(
            is_deleted=False, stock_item__is_active=True).select_related("stock_item")
        total_val = low = out = 0
        for lv in levels:
            q = _f(lv.quantity)
            total_val += q * _f(lv.stock_item.avg_cost_price)
            if q <= 0:
                out += 1
            elif q <= _f(lv.stock_item.reorder_point):
                low += 1
        expiring = StockBatch.objects.filter(
            is_deleted=False, current_quantity__gt=0,
            expiry_date__gt=today_local, expiry_date__lte=today_local + timedelta(days=14),
        ).count()
        cash = CashRegister.objects.first()
        return {
            "now": _iso(now),
            "today": today_local.isoformat(),
            "today_sales": {
                "orders": t_agg["cnt"] or 0,
                "paid_revenue_uzs": _f(t_agg["revenue"]),
                "paid_orders": t_agg["paid"] or 0,
                "unpaid_orders": t_agg["unpaid"] or 0,
            },
            "open_shifts_count": len(open_list),
            "open_shifts": open_list,
            "cash_register_balance_uzs": _f(cash.current_balance) if cash else 0.0,
            "stock": {
                "total_levels": levels.count(),
                "total_value_uzs": round(total_val, 2),
                "low_stock_count": low,
                "out_of_stock_count": out,
                "expiring_14d": expiring,
            },
            "counts": {
                "products": Product.objects.filter(is_deleted=False).count(),
                "categories": Category.objects.filter(is_deleted=False).count(),
                "customers": Customer.objects.filter(is_deleted=False).count(),
                "users": User.objects.filter(is_deleted=False).count(),
            },
            "hint": "Use list_orders / get_order / get_open_shifts / get_shift / list_cashiers / list_products / list_stock / sales_report / business_analytics for any detail.",
        }

    @classmethod
    def _t_list_orders(cls, args):
        qs = (Order.objects.filter(is_deleted=False)
              .select_related("cashier", "customer", "table", "place")
              .prefetch_related("items__product"))
        d = _parse_date(args.get("date"))
        if d:
            qs = qs.filter(created_at__date=d)
        df, dt = _parse_date(args.get("date_from")), _parse_date(args.get("date_to"))
        if df:
            qs = qs.filter(created_at__date__gte=df)
        if dt:
            qs = qs.filter(created_at__date__lte=dt)
        if args.get("status"):
            qs = qs.filter(status=str(args["status"]).upper())
        if args.get("order_type"):
            qs = qs.filter(order_type=str(args["order_type"]).upper())
        if args.get("payment_method"):
            qs = qs.filter(payment_method=str(args["payment_method"]).upper())
        if args.get("is_paid") is not None:
            qs = qs.filter(is_paid=bool(args["is_paid"]))
        if args.get("cashier_id"):
            qs = qs.filter(cashier_id=args["cashier_id"])
        if args.get("customer_phone"):
            p = str(args["customer_phone"])
            qs = qs.filter(Q(phone_number__icontains=p) | Q(customer__phone_number__icontains=p))
        if args.get("product_name"):
            qs = qs.filter(items__product__name__icontains=str(args["product_name"]),
                           items__is_deleted=False).distinct()
        total = qs.count()
        limit = _clamp(args.get("limit"), 50, 1, MAX_ORDERS)
        offset = _clamp(args.get("offset"), 0, 0, _OFFSET_MAX)
        rows = qs.order_by("-created_at")[offset:offset + limit]
        orders = [_order_summary(o) for o in rows]
        return {
            "total_matching": total,
            "returned": len(orders),
            "offset": offset,
            "limit": limit,
            "orders": orders,
        }

    @classmethod
    def _t_get_order(cls, args):
        base = (Order.objects.filter(is_deleted=False)
                .select_related("cashier", "customer", "table", "place", "delivery_person")
                .prefetch_related("items__product__category", "payments"))
        o = None
        if args.get("order_id"):
            o = base.filter(id=args["order_id"]).first()
        elif args.get("display_id"):
            q = base.filter(display_id=args["display_id"])
            d = _parse_date(args.get("date"))
            if d:
                q = q.filter(created_at__date=d)
            o = q.order_by("-created_at").first()
        if o is None:
            return {"error": "order not found", "args": args}
        return _order_full(o)

    @classmethod
    def _t_open_shifts(cls, args):
        qs = (Shift.objects.filter(is_deleted=False, status=Shift.Status.ACTIVE)
              .select_related("user", "shift_template", "reconciliation").order_by("-start_time"))
        total = qs.count()
        data = [_shift_dict(s) for s in qs[:MAX_LIST]]
        return {"open_shifts_count": total, "returned": len(data), "open_shifts": data}

    @classmethod
    def _t_list_shifts(cls, args):
        qs = Shift.objects.filter(is_deleted=False).select_related("user", "shift_template", "reconciliation")
        if args.get("status"):
            qs = qs.filter(status=str(args["status"]).upper())
        if args.get("cashier_id"):
            qs = qs.filter(user_id=args["cashier_id"])
        d = _parse_date(args.get("date"))
        if d:
            qs = qs.filter(start_time__date=d)
        df, dt = _parse_date(args.get("date_from")), _parse_date(args.get("date_to"))
        if df:
            qs = qs.filter(start_time__date__gte=df)
        if dt:
            qs = qs.filter(start_time__date__lte=dt)
        total = qs.count()
        limit = _clamp(args.get("limit"), 50, 1, MAX_ORDERS)
        offset = _clamp(args.get("offset"), 0, 0, _OFFSET_MAX)
        rows = qs.order_by("-start_time")[offset:offset + limit]
        return {"total_matching": total, "returned": len(rows), "offset": offset, "limit": limit,
                "shifts": [_shift_dict(s) for s in rows]}

    @classmethod
    def _t_get_shift(cls, args):
        s = (Shift.objects.filter(is_deleted=False, id=args.get("shift_id"))
             .select_related("user", "shift_template", "reconciliation").first())
        if s is None:
            return {"error": "shift not found", "args": args}
        return _shift_dict(s, with_orders=True)

    @classmethod
    def _t_list_cashiers(cls, args):
        qs = User.objects.filter(is_deleted=False)
        if args.get("role"):
            qs = qs.filter(role=str(args["role"]).upper())
        if args.get("only_on_shift"):
            # Filter at the DB level (before slicing) so an on-shift cashier past
            # the page cap isn't silently dropped from a "who is working" answer.
            qs = qs.filter(shifts__is_deleted=False,
                           shifts__status=Shift.Status.ACTIVE).distinct()
        total = qs.count()
        limit = _clamp(args.get("limit"), MAX_LIST, 1, MAX_LIST)
        offset = _clamp(args.get("offset"), 0, 0, _OFFSET_MAX)
        rows = list(qs.order_by("first_name", "last_name")[offset:offset + limit])
        return {"total_matching": total, "returned": len(rows), "offset": offset, "limit": limit,
                "cashiers": [_cashier_dict(u) for u in rows]}

    @classmethod
    def _t_get_cashier(cls, args):
        u = User.objects.filter(is_deleted=False, id=args.get("cashier_id")).first()
        if u is None:
            return {"error": "cashier not found", "args": args}
        data = _cashier_dict(u)
        cutoff = timezone.now().date() - timedelta(days=30)
        perf = Order.objects.filter(
            is_deleted=False, cashier=u, created_at__date__gte=cutoff
        ).aggregate(
            orders=Count("id"),
            revenue=Sum("total_amount", filter=Q(is_paid=True)),
            avg=Avg("total_amount"),
            completed=Count("id", filter=Q(status="COMPLETED")),
            canceled=Count("id", filter=Q(status="CANCELED")),
        )
        data["performance_30d"] = {
            "orders": perf["orders"] or 0,
            "paid_revenue_uzs": _f(perf["revenue"]),
            "avg_order_uzs": _f(perf["avg"]),
            "completed": perf["completed"] or 0,
            "canceled": perf["canceled"] or 0,
        }
        shifts = (Shift.objects.filter(is_deleted=False, user=u)
                  .select_related("shift_template", "reconciliation").order_by("-start_time")[:10])
        data["recent_shifts"] = [_shift_dict(s) for s in shifts]
        recent = (Order.objects.filter(is_deleted=False, cashier=u)
                  .select_related("customer", "table", "place")
                  .prefetch_related("items__product").order_by("-created_at")[:20])
        data["recent_orders"] = [_order_summary(o) for o in recent]
        return data

    @classmethod
    def _t_list_products(cls, args):
        qs = Product.objects.filter(is_deleted=False).select_related("category")
        if args.get("search"):
            qs = qs.filter(name__icontains=str(args["search"]))
        if args.get("category"):
            qs = qs.filter(category__name__icontains=str(args["category"]))
        total = qs.count()
        limit = _clamp(args.get("limit"), MAX_LIST, 1, MAX_LIST)
        offset = _clamp(args.get("offset"), 0, 0, _OFFSET_MAX)
        rows = qs.order_by("category__name", "name")[offset:offset + limit]
        return {"total_matching": total, "returned": len(rows), "offset": offset, "limit": limit,
                "products": [_product_dict(p) for p in rows]}

    @classmethod
    def _t_list_stock(cls, args):
        qs = (StockLevel.objects.filter(is_deleted=False)
              .select_related("stock_item", "stock_item__base_unit", "location"))
        if not args.get("include_inactive"):
            qs = qs.filter(stock_item__is_active=True)
        if args.get("search"):
            qs = qs.filter(stock_item__name__icontains=str(args["search"]))
        if args.get("location"):
            qs = qs.filter(location__name__icontains=str(args["location"]))
        if args.get("low_only"):
            qs = qs.filter(quantity__lte=F("stock_item__reorder_point"))
        if args.get("out_only"):
            qs = qs.filter(quantity__lte=0)
        total = qs.count()
        limit = _clamp(args.get("limit"), MAX_LIST, 1, MAX_LIST)
        offset = _clamp(args.get("offset"), 0, 0, _OFFSET_MAX)
        rows = list(qs.order_by("stock_item__name")[offset:offset + limit])
        items = [_stock_dict(lv) for lv in rows]
        return {
            "total_matching": total,
            "returned": len(items),
            "offset": offset,
            "limit": limit,
            "total_value_uzs": round(sum(i["value_uzs"] for i in items), 2),
            "items": items,
        }

    @classmethod
    def _t_sales_report(cls, args):
        d = _parse_date(args.get("date"))
        df, dt = _parse_date(args.get("date_from")), _parse_date(args.get("date_to"))
        if d:
            df = dt = d
        if not df and not dt:
            dt = timezone.now().date()
            df = dt - timedelta(days=29)
        elif df and not dt:
            dt = df
        elif dt and not df:
            df = dt

        orders = Order.objects.filter(is_deleted=False, created_at__date__gte=df, created_at__date__lte=dt)
        items = OrderItem.objects.filter(
            is_deleted=False, order__is_deleted=False,
            order__created_at__date__gte=df, order__created_at__date__lte=dt,
        )

        totals = orders.aggregate(
            orders=Count("id"),
            revenue=Sum("total_amount", filter=Q(is_paid=True)),
            gross=Sum("total_amount"),
            avg=Avg("total_amount"),
            paid=Count("id", filter=Q(is_paid=True)),
            unpaid=Count("id", filter=Q(is_paid=False)),
            discount=Sum("discount_amount"),
        )
        by_day = [{
            "date": r["day"].isoformat() if r["day"] else None,
            "orders": r["c"], "revenue_uzs": _f(r["rev"]),
        } for r in orders.annotate(day=TruncDate("created_at")).values("day").annotate(
            c=Count("id"), rev=Sum("total_amount", filter=Q(is_paid=True))).order_by("day")]
        by_cashier = [{
            "cashier": f"{r['cashier__first_name']} {r['cashier__last_name']}".strip(),
            "cashier_id": r["cashier__id"], "orders": r["c"], "revenue_uzs": _f(r["rev"]),
        } for r in orders.filter(cashier__isnull=False).values(
            "cashier__id", "cashier__first_name", "cashier__last_name").annotate(
            c=Count("id"), rev=Sum("total_amount", filter=Q(is_paid=True))).order_by("-rev")]
        by_category = [{
            "category": r["product__category__name"], "qty": r["q"], "revenue_uzs": _f(r["rev"]),
        } for r in items.values("product__category__name").annotate(
            q=Sum("quantity"), rev=Sum(F("quantity") * F("price"))).order_by("-rev")[:25]]
        top_products = [{
            "name": r["product__name"], "qty": r["q"], "revenue_uzs": _f(r["rev"]),
        } for r in items.values("product__name").annotate(
            q=Sum("quantity"), rev=Sum(F("quantity") * F("price"))).order_by("-rev")[:25]]
        by_method = {r["payment_method"] or "UNSET": {"orders": r["c"], "revenue_uzs": _f(r["rev"])}
                     for r in orders.values("payment_method").annotate(
                         c=Count("id"), rev=Sum("total_amount", filter=Q(is_paid=True)))}
        by_type = {r["order_type"]: r["c"] for r in orders.values("order_type").annotate(c=Count("id"))}

        result = {
            "date_from": df.isoformat(),
            "date_to": dt.isoformat(),
            "totals": {
                "orders": totals["orders"] or 0,
                "paid_revenue_uzs": _f(totals["revenue"]),
                "gross_uzs": _f(totals["gross"]),
                "avg_order_uzs": _f(totals["avg"]),
                "paid_orders": totals["paid"] or 0,
                "unpaid_orders": totals["unpaid"] or 0,
                "total_discount_uzs": _f(totals["discount"]),
                "items_sold": items.aggregate(q=Sum("quantity"))["q"] or 0,
            },
            "by_day": by_day,
            "by_cashier": by_cashier,
            "by_category": by_category,
            "top_products": top_products,
            "by_payment_method": by_method,
            "by_order_type": by_type,
        }
        if df == dt:
            result["by_hour"] = [{
                "hour": r["h"].strftime("%H:%M") if r["h"] else None,
                "orders": r["c"], "revenue_uzs": _f(r["rev"]),
            } for r in orders.annotate(h=TruncHour("created_at")).values("h").annotate(
                c=Count("id"), rev=Sum("total_amount", filter=Q(is_paid=True))).order_by("h")]
        return result

    @classmethod
    def _t_analytics(cls, args):
        from stock.services.ai_assistant_service import AIStockAssistant
        days = int(args.get("days") or 30)
        kind = str(args.get("kind") or "all").lower()
        builders = {
            "abc": lambda: AIStockAssistant._get_abc_analysis(days),
            "xyz": lambda: AIStockAssistant._get_xyz_analysis(days),
            "abc_xyz": lambda: AIStockAssistant._get_abc_xyz_matrix(days),
            "menu": lambda: AIStockAssistant._get_menu_engineering(days),
            "profitability": lambda: AIStockAssistant._get_profitability_analysis(days),
            "inventory_health": lambda: AIStockAssistant._get_inventory_health(days),
            "sales_velocity": lambda: AIStockAssistant._get_sales_velocity(days),
        }
        if kind == "all":
            return {k: _cap_analytics(b()) for k, b in builders.items()}
        b = builders.get(kind)
        if b is None:
            return {"error": f"unknown analytics kind: {kind}", "available": list(builders)}
        return {kind: _cap_analytics(b())}


__all__ = ["AIToolbox"]
