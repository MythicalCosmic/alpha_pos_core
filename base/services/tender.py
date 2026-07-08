"""Canonical tender attribution — the ONE place tender arithmetic lives.

STORAGE keeps acquirer detail (UZCARD / HUMO / CARD) so a merchant can still
reconcile each acquirer's bank statement. PRESENTATION has exactly three tenders
— ``cash``, ``card``, ``payme`` — plus an ``unknown`` bucket that exists so a
breakdown ALWAYS sums to revenue. ``MIXED`` is a legacy roll-up marker on
``Order.payment_method``; it is NEVER a bucket.

CASH IS DERIVED, NEVER SUMMED. ``OrderPayment`` stores the cash TENDERED, which
may exceed the bill (the customer's change), so::

    cash = total_amount - Σ(non-cash lines)

That is exactly what the till credits to the drawer (customers.order_service
``mark_as_paid``: ``cash_to_drawer = effective_total - noncash_sum``) and what the
cancel path reverses. Summing the raw CASH lines over-reports cash by the change.

An order is attributed by this ladder (first match wins):

  1. usable ``OrderPayment`` lines            -> cash = total - noncash
  2. no lines, but PAID ``CourierPayment``    -> split via PROVIDER_TO_METHOD
  3. no lines, payment_method NULL | CASH     -> cash  = total   (documented legacy)
  4. no lines, payment_method UZCARD|HUMO|CARD-> card  = total
  5. no lines, payment_method PAYME           -> payme = total
  6. anything else — ``MIXED`` without lines, an unrecognised method, or
     Σnoncash > total                          -> unknown = total, and log.error

``unknown`` must be zero in a healthy system. A non-zero value is an alertable
data defect (e.g. an Order that synced without its OrderPayment children), never
a display value. NEVER silently fold it into cash: cash is the residual, so it
would absorb the defect invisibly.
"""
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

ZERO = Decimal('0.00')
CASH = 'CASH'

# POSITIVE whitelist. Never `.exclude(method='CASH')`: OrderPayment.method inherits
# Order.PaymentMethod.choices, so a 'MIXED' row is model-legal and an exclude()
# would silently count it as non-cash and shrink the derived cash residual.
NONCASH_METHODS = frozenset({'UZCARD', 'HUMO', 'CARD', 'PAYME'})
KNOWN_METHODS = NONCASH_METHODS | {CASH}

# Stored tender -> presentation bucket. 'CARD' is accepted because smartfood
# already writes it and a till may start emitting it.
_BUCKET = {
    'CASH': 'cash',
    'UZCARD': 'card', 'HUMO': 'card', 'CARD': 'card',
    'PAYME': 'payme',
}
# Acquirer-level detail kept alongside the collapsed `card` bucket.
CARD_METHODS = ('UZCARD', 'HUMO', 'CARD')
BUCKETS = ('cash', 'card', 'payme', 'unknown')


def normalize_method(method):
    """Stored tender, upper-cased; NULL/'' means CASH (documented legacy)."""
    return (method or CASH).strip().upper()


def bucket_for(method):
    """Presentation bucket for a stored tender, or None when unrecognised."""
    return _BUCKET.get(normalize_method(method))


def empty_split():
    return {b: ZERO for b in BUCKETS}


def empty_detail():
    return {m: ZERO for m in CARD_METHODS}


def _dec(v):
    return v if isinstance(v, Decimal) else Decimal(str(v or 0))


def split_from_rows(total, payment_method, op_rows=(), courier_rows=(), order_id=None):
    """Pure ladder over plain data. Returns (split, card_detail).

    `op_rows` / `courier_rows` are iterables of (method, amount) — courier providers
    must already be mapped to a tender. The returned split sums EXACTLY to `total`.
    """
    total = _dec(total)
    split, detail = empty_split(), empty_detail()
    if total <= ZERO:
        return split, detail            # nothing to attribute (incl. 100% discount)

    def _derive(rows, source):
        """cash = total - noncash, given tender rows. Returns None when unusable."""
        noncash, per = ZERO, {}
        for method, amount in rows:
            m = normalize_method(method)
            if m not in KNOWN_METHODS:
                logger.error('tender: order %s has %s line with unrecognised method '
                             '%r -> unknown', order_id, source, method)
                return None
            if m in NONCASH_METHODS:
                amt = _dec(amount)
                noncash += amt
                per[m] = per.get(m, ZERO) + amt
        if noncash > total:
            logger.error('tender: order %s %s noncash=%s exceeds total=%s -> unknown',
                         order_id, source, noncash, total)
            return None
        s, d = empty_split(), empty_detail()
        for m, amt in per.items():
            s[_BUCKET[m]] += amt
            if m in d:
                d[m] += amt
        s['cash'] = total - noncash     # derived: ignores the customer's change
        return s, d

    # 1. till-written payment lines — the only fully trusted source
    op_rows = list(op_rows)
    if op_rows:
        got = _derive(op_rows, 'OrderPayment')
        if got:
            return got
        split['unknown'] = total
        return split, detail

    # 2. courier collection at the door (writes no OrderPayment today)
    courier_rows = list(courier_rows)
    if courier_rows:
        got = _derive(courier_rows, 'CourierPayment')
        if got:
            return got
        split['unknown'] = total
        return split, detail

    # 3-5. no lines at all: fall back to the rolled-up method
    bucket = bucket_for(payment_method)
    if bucket:
        split[bucket] = total
        if bucket == 'card':
            m = normalize_method(payment_method)
            if m in detail:
                detail[m] = total
        return split, detail

    # 6. MIXED without lines, or an unrecognised method — UNRESOLVABLE. Never guess.
    logger.error('tender: order %s payment_method=%r with no payment lines '
                 '-> unknown (unresolvable)', order_id, payment_method)
    split['unknown'] = total
    return split, detail


def _courier_rows_by_order(order_ids):
    """{order_id: [(tender, amount)]} for PAID courier collections. Empty when the
    couriers app is not installed in this edition."""
    if not order_ids:
        return {}
    try:
        from couriers.models import CourierPayment
    except Exception:  # noqa: BLE001 — edition without the courier app
        return {}
    out = {}
    try:
        rows = CourierPayment.objects.filter(
            order_id__in=list(order_ids), status='PAID',
        ).values_list('order_id', 'provider', 'amount')
    except Exception:  # noqa: BLE001 — table missing on a half-migrated DB
        logger.exception('tender: courier payment lookup failed')
        return {}
    mapping = CourierPayment.PROVIDER_TO_METHOD
    for oid, provider, amount in rows:
        out.setdefault(oid, []).append((mapping.get(provider, provider), amount))
    return out


def order_tender_split(order):
    """(split, card_detail) for ONE order. Sums exactly to order.total_amount."""
    from base.models import OrderPayment
    ops = list(OrderPayment.objects.filter(is_deleted=False, order_id=order.id)
               .values_list('method', 'amount'))
    courier = [] if ops else _courier_rows_by_order([order.id]).get(order.id, [])
    return split_from_rows(order.total_amount, order.payment_method,
                           ops, courier, order_id=order.id)


def breakdown_for_orders(order_qs):
    """Aggregate {cash, card, payme, unknown} + card_detail over an Order queryset.

    The caller builds ONE queryset (window / cashier / paid / not-cancelled filters)
    and passes it in; the payment rows are derived FROM it, so both halves can never
    drift apart. Guarantees cash+card+payme+unknown == Sum(total_amount) exactly.

    Never annotates Sum('total_amount') alongside Sum('payments__amount') — that
    fans the order total out by its payment-row count.
    """
    from base.models import OrderPayment

    rows = list(order_qs.values('id', 'total_amount', 'payment_method'))
    if not rows:
        return empty_split(), empty_detail()
    ids = [r['id'] for r in rows]

    ops = {}
    # OrderPayment.objects is a SyncManager and does NOT filter soft-deletes; spell
    # it out (mirrors cashbox/services/drawer.py).
    for oid, method, amount in OrderPayment.objects.filter(
            is_deleted=False, order_id__in=ids).values_list('order_id', 'method', 'amount'):
        ops.setdefault(oid, []).append((method, amount))

    courier = _courier_rows_by_order([r['id'] for r in rows if r['id'] not in ops])

    split, detail = empty_split(), empty_detail()
    for r in rows:
        oid = r['id']
        s, d = split_from_rows(r['total_amount'], r['payment_method'],
                               ops.get(oid, ()), courier.get(oid, ()), order_id=oid)
        for k in BUCKETS:
            split[k] += s[k]
        for k in CARD_METHODS:
            detail[k] += d[k]
    return split, detail


def noncash_total_for_orders(order_qs):
    """Σ(non-cash OrderPayment lines) over an order queryset — the exact quantity the
    drawer must subtract from revenue to get physical cash (the raw CASH lines are
    the TENDERED amount and include the change)."""
    from base.models import OrderPayment
    from django.db.models import Sum
    return OrderPayment.objects.filter(
        is_deleted=False, order__in=order_qs, method__in=list(NONCASH_METHODS),
    ).aggregate(s=Sum('amount'))['s'] or ZERO


def unattributed_orders(order_qs=None):
    """CANARY: paid, non-cancelled, non-deleted orders that have NO OrderPayment rows
    and a non-cash rolled-up method — i.e. money a breakdown cannot attribute from
    payment lines. Must be 0. A non-zero count is the detector for the sync
    dead-letter hole (an Order lands on the cloud, its payments never do)."""
    from django.db.models import Count, Q
    from base.models import Order
    qs = order_qs if order_qs is not None else Order.objects.filter(
        is_deleted=False, is_paid=True).exclude(status='CANCELED')
    return (qs.annotate(_n=Count('payments', filter=Q(payments__is_deleted=False)))
              .filter(_n=0)
              .exclude(payment_method=CASH)
              .exclude(payment_method__isnull=True))
