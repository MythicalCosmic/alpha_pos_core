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

  1. complete till and/or external payment lines -> cash = total - all noncash
  2. no lines, payment_method NULL | CASH      -> cash  = total   (documented legacy)
  3. no lines, payment_method UZCARD|HUMO|CARD -> card  = total
  4. no lines, payment_method PAYME            -> payme = total
  5. anything else — ``MIXED`` without lines, an unrecognised method, or
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

    `op_rows` / `courier_rows` are iterables of (method, amount). The latter is
    the exact non-drawer collection stream: canonical ExternalOrderPayment rows
    plus a de-duplicated legacy CourierPayment fallback. The returned split
    sums EXACTLY to `total`.
    """
    total = _dec(total)
    split, detail = empty_split(), empty_detail()
    if total <= ZERO:
        return split, detail            # nothing to attribute (incl. 100% discount)

    def _derive(rows, source):
        """Derive bill cash from complete tender evidence, or fail closed.

        Raw cash may exceed the residual because it includes customer change,
        so it is never summed as revenue. It must still *cover* the residual.
        Without that check, a dropped child row made the missing amount
        silently appear as cash.
        """
        noncash, cash_tendered, per = ZERO, ZERO, {}
        for method, amount in rows:
            m = normalize_method(method)
            if m not in KNOWN_METHODS:
                logger.error('tender: order %s has %s line with unrecognised method '
                             '%r -> unknown', order_id, source, method)
                return None
            amt = _dec(amount)
            if amt < ZERO:
                logger.error(
                    'tender: order %s has negative %s line %s=%s -> unknown',
                    order_id, source, m, amt,
                )
                return None
            if m in NONCASH_METHODS:
                noncash += amt
                per[m] = per.get(m, ZERO) + amt
            elif m == CASH:
                cash_tendered += amt
        if noncash > total:
            logger.error('tender: order %s %s noncash=%s exceeds total=%s -> unknown',
                         order_id, source, noncash, total)
            return None
        residual_cash = total - noncash
        if residual_cash > ZERO and cash_tendered < residual_cash:
            logger.error(
                'tender: order %s %s cash evidence=%s does not cover '
                'residual=%s -> unknown',
                order_id, source, cash_tendered, residual_cash,
            )
            return None
        s, d = empty_split(), empty_detail()
        for m, amt in per.items():
            s[_BUCKET[m]] += amt
            if m in d:
                d[m] += amt
        s['cash'] = residual_cash       # derived: ignores the customer's change
        return s, d

    # 1. Concrete money rows. A delivery can be split between a till payment
    # and collection at the door, so neither source may hide the other.
    op_rows = list(op_rows)
    courier_rows = list(courier_rows)
    if op_rows or courier_rows:
        # A till CASH line is cash tendered and may contain change. External
        # collections are exact settled amounts, so they may never borrow the
        # till-CASH change rule to hide an over-collection.
        external_total = ZERO
        for method, amount in courier_rows:
            normalized = normalize_method(method)
            amount = _dec(amount)
            if normalized not in KNOWN_METHODS or amount <= ZERO:
                logger.error(
                    'tender: order %s has invalid external payment %r=%s '
                    '-> unknown', order_id, method, amount,
                )
                split['unknown'] = total
                return split, detail
            external_total += amount
        till_noncash = sum(
            (_dec(amount) for method, amount in op_rows
             if normalize_method(method) in NONCASH_METHODS
             and _dec(amount) > ZERO),
            ZERO,
        )
        if external_total + till_noncash > total:
            logger.error(
                'tender: order %s exact external=%s plus till noncash=%s '
                'exceeds total=%s -> unknown',
                order_id, external_total, till_noncash, total,
            )
            split['unknown'] = total
            return split, detail
        got = _derive(op_rows + courier_rows, 'payment')
        if got:
            return got
        split['unknown'] = total
        return split, detail

    # 2-4. no lines at all: fall back to the rolled-up method
    bucket = bucket_for(payment_method)
    if bucket:
        split[bucket] = total
        if bucket == 'card':
            m = normalize_method(payment_method)
            if m in detail:
                detail[m] = total
        return split, detail

    # 5. MIXED without lines, or an unrecognised method — UNRESOLVABLE. Never guess.
    logger.error('tender: order %s payment_method=%r with no payment lines '
                 '-> unknown (unresolvable)', order_id, payment_method)
    split['unknown'] = total
    return split, detail


def _courier_rows_by_order(order_ids):
    """Return exact non-drawer collection rows, de-duplicated by event ID.

    ``ExternalOrderPayment`` is the canonical synced evidence available in
    every edition. The optional courier app remains a compatibility fallback
    for historical rows created before that event existed; once its external_id
    has a canonical mirror, only the synced row is counted.
    """
    if not order_ids:
        return {}
    from base.models import ExternalOrderPayment

    ids = list(order_ids)
    out = {}
    mirrored = set()
    for oid, method, amount, source_id in ExternalOrderPayment.objects.filter(
        order_id__in=ids,
        source=ExternalOrderPayment.Source.COURIER,
        is_deleted=False,
    ).values_list('order_id', 'method', 'amount', 'source_id'):
        out.setdefault(oid, []).append((method, amount))
        mirrored.add((oid, str(source_id or '')))

    try:
        from couriers.models import CourierPayment
    except Exception:  # noqa: BLE001 — edition without the courier app
        return out
    try:
        rows = CourierPayment.objects.filter(
            order_id__in=ids, status__in=['PAID', 'REFUNDED'],
        ).values_list('order_id', 'provider', 'amount', 'external_id')
    except Exception:  # noqa: BLE001 — table missing on a half-migrated DB
        logger.exception('tender: courier payment lookup failed')
        return out
    mapping = CourierPayment.PROVIDER_TO_METHOD
    for oid, provider, amount, external_id in rows:
        if (oid, str(external_id or '')) in mirrored:
            continue
        out.setdefault(oid, []).append((mapping.get(provider, provider), amount))
    return out


def order_tender_split(order):
    """(split, card_detail) for ONE order. Sums exactly to order.total_amount."""
    split, detail, _drawer_cash = order_tender_sources(order)
    return split, detail


def order_tender_sources(order):
    """Return ``(split, card_detail, drawer_cash)`` for one paid order.

    ``split['cash']`` is all cash tender, including courier cash collected at
    the door. ``drawer_cash`` is the subset evidenced by OrderPayment CASH
    rows, capped at the derived bill residual so tendered change is excluded.
    With no concrete evidence, legacy CASH is treated as drawer cash; once a
    courier row exists we never guess that its cash entered the POS drawer.
    """
    from base.models import OrderPayment
    ops = list(OrderPayment.objects.filter(is_deleted=False, order_id=order.id)
               .values_list('method', 'amount'))
    courier = _courier_rows_by_order([order.id]).get(order.id, [])
    split, detail = split_from_rows(
        order.total_amount, order.payment_method,
        ops, courier, order_id=order.id,
    )
    drawer_cash = _drawer_cash_from_sources(
        order.total_amount, split, ops, courier,
    )
    return split, detail, drawer_cash


def _drawer_cash_from_sources(total, split, ops, courier):
    """Physical till cash represented by already-loaded tender evidence."""
    if not ops and not courier:
        return split['cash']
    tendered = sum(
        (_dec(amount) for method, amount in ops
         if normalize_method(method) == CASH and _dec(amount) > ZERO),
        ZERO,
    )
    till_noncash = sum(
        (_dec(amount) for method, amount in ops
         if normalize_method(method) in NONCASH_METHODS
         and _dec(amount) > ZERO),
        ZERO,
    )
    courier_collected = sum(
        (max(_dec(amount), ZERO) for _method, amount in courier), ZERO,
    )
    drawer_bill_residual = max(
        _dec(total) - till_noncash - courier_collected,
        ZERO,
    )
    return min(split['cash'], tendered, drawer_bill_residual)


def breakdown_sources_for_orders(order_qs):
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
        return empty_split(), empty_detail(), ZERO
    ids = [r['id'] for r in rows]

    ops = {}
    # OrderPayment.objects is a SyncManager and does NOT filter soft-deletes; spell
    # it out (mirrors cashbox/services/drawer.py).
    for oid, method, amount in OrderPayment.objects.filter(
            is_deleted=False, order_id__in=ids).values_list('order_id', 'method', 'amount'):
        ops.setdefault(oid, []).append((method, amount))

    courier = _courier_rows_by_order(ids)

    split, detail, drawer_cash = empty_split(), empty_detail(), ZERO
    for r in rows:
        oid = r['id']
        order_ops = ops.get(oid, ())
        order_courier = courier.get(oid, ())
        s, d = split_from_rows(
            r['total_amount'], r['payment_method'],
            order_ops, order_courier, order_id=oid,
        )
        for k in BUCKETS:
            split[k] += s[k]
        for k in CARD_METHODS:
            detail[k] += d[k]
        drawer_cash += _drawer_cash_from_sources(
            r['total_amount'], s, order_ops, order_courier,
        )
    return split, detail, drawer_cash


def breakdown_for_orders(order_qs):
    """Aggregate analytics tender split and acquirer detail for orders."""
    split, detail, _drawer_cash = breakdown_sources_for_orders(order_qs)
    return split, detail


def drawer_cash_for_orders(order_qs):
    """Aggregate only cash that physically entered a POS drawer."""
    _split, _detail, drawer_cash = breakdown_sources_for_orders(order_qs)
    return drawer_cash


def breakdown_for_refunds(refund_qs):
    """Aggregate frozen tender buckets for an OrderRefund event queryset."""
    split, detail = empty_split(), empty_detail()
    for row in refund_qs.values(
        'cash_amount', 'card_amount', 'payme_amount', 'unknown_amount',
        'card_detail',
    ).iterator():
        split['cash'] += _dec(row['cash_amount'])
        split['card'] += _dec(row['card_amount'])
        split['payme'] += _dec(row['payme_amount'])
        split['unknown'] += _dec(row['unknown_amount'])
        frozen_detail = row.get('card_detail') or {}
        for method in CARD_METHODS:
            detail[method] += _dec(frozen_detail.get(method))
    return split, detail


def net_breakdown(sale_order_qs, refund_qs):
    """Net tender movement = sale events minus dated refund events."""
    sales, sale_detail = breakdown_for_orders(sale_order_qs)
    refunds, refund_detail = breakdown_for_refunds(refund_qs)
    return (
        {key: sales[key] - refunds[key] for key in BUCKETS},
        {key: sale_detail[key] - refund_detail[key] for key in CARD_METHODS},
    )


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
    """CANARY: paid, non-deleted orders that have no concrete payment rows
    and a non-cash rolled-up method — i.e. money a breakdown cannot attribute from
    payment lines. Must be 0. A non-zero count is the detector for the sync
    dead-letter hole (an Order lands on the cloud, its payments never do)."""
    from django.db.models import Count, Q
    from base.models import ExternalOrderPayment, Order
    qs = order_qs if order_qs is not None else Order.objects.filter(
        is_deleted=False, is_paid=True)
    missing = (qs.annotate(_n=Count('payments', filter=Q(payments__is_deleted=False)))
                 .filter(_n=0)
                 .exclude(payment_method=CASH)
                 .exclude(payment_method__isnull=True))
    external_orders = ExternalOrderPayment.objects.filter(
        is_deleted=False, order_id__in=missing.values('pk'),
    ).values('order_id')
    missing = missing.exclude(pk__in=external_orders)
    # A courier-only delivery correctly has no till OrderPayment row. Do not
    # report it as missing when its PAID courier collection is present.
    try:
        from couriers.models import CourierPayment
        courier_orders = CourierPayment.objects.filter(
            status='PAID', order_id__in=missing.values('pk'),
        ).values('order_id')
        missing = missing.exclude(pk__in=courier_orders)
    except Exception:  # noqa: BLE001 - the core edition has no courier table
        pass
    return missing


def tender_integrity_issues(order_qs, *, require_concrete=False):
    """List paid orders whose tender evidence is missing or incomplete.

    ``require_concrete`` is used by the post-upgrade shift lifecycle. Legacy
    reports may still interpret a CASH header without child rows, but a newly
    settlement-eligible shift must prove every positive sale with either an
    OrderPayment or a completed CourierPayment before it can be handed over.
    """
    rows = list(order_qs.values('id', 'total_amount', 'payment_method'))
    if not rows:
        return []
    ids = [row['id'] for row in rows]

    from base.models import OrderPayment
    till = {}
    for order_id, method, amount in OrderPayment.objects.filter(
        is_deleted=False, order_id__in=ids,
    ).values_list('order_id', 'method', 'amount'):
        till.setdefault(order_id, []).append((method, amount))
    courier = _courier_rows_by_order(ids)

    issues = []
    for row in rows:
        order_id = row['id']
        payment_rows = till.get(order_id, ())
        courier_rows = courier.get(order_id, ())
        if not payment_rows and not courier_rows:
            if (
                _dec(row['total_amount']) > ZERO
                and (
                    require_concrete
                    or normalize_method(row['payment_method']) != CASH
                )
            ):
                issues.append({
                    'order_id': order_id,
                    'amount': _dec(row['total_amount']),
                    'payment_method': row['payment_method'],
                    'reason': 'no concrete payment evidence',
                })
            continue
        split, _detail = split_from_rows(
            row['total_amount'], row['payment_method'],
            payment_rows, courier_rows, order_id=order_id,
        )
        if split['unknown']:
            issues.append({
                'order_id': order_id,
                'amount': _dec(row['total_amount']),
                'payment_method': row['payment_method'],
                'reason': 'invalid or incomplete payment evidence',
            })
    return issues
