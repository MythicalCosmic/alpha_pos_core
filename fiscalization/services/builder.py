"""Turn an Order into the provider-neutral receipt payload (see providers/base).

Money is converted to **tiyin** (1 so'm = 100 tiyin) — the unit every Uzbek OFD
API expects. VAT is computed as the VAT-inclusive portion of each line at the
configured QQS rate (0 if the business isn't VAT-registered).
"""
from decimal import Decimal, ROUND_HALF_UP


def _to_tiyin(amount):
    return int((Decimal(str(amount or 0)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _vat_portion(line_total_tiyin, vat_percent):
    if not vat_percent:
        return 0
    v = Decimal(str(vat_percent))
    portion = (Decimal(line_total_tiyin) * v) / (Decimal('100') + v)
    return int(portion.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def build_receipt_payload(order, tenant, receipt_type='SALE'):
    vat_percent = tenant.get('vat_percent', 0) or 0

    # Raw (pre-discount) extended line totals in tiyin.
    raw_lines = [
        (item, _to_tiyin(Decimal(str(item.price)) * item.quantity))
        for item in order.items.select_related('product').all()
    ]
    gross = sum(lt for _, lt in raw_lines)
    total = _to_tiyin(order.total_amount)

    # Apportion any order-level discount across the lines so the line totals sum
    # to the amount actually charged and VAT is computed on the DISCOUNTED line
    # amounts. Previously each line used its pre-discount total while `total`
    # used the discounted figure, so on every discounted sale the receipt
    # over-reported both line amounts and VAT versus the real transaction — a
    # fiscal-compliance defect. The last line absorbs the rounding remainder so
    # the lines reconcile exactly to `total`.
    apportion = gross > 0 and total != gross
    items = []
    running = 0
    last_idx = len(raw_lines) - 1
    for idx, (item, raw_total) in enumerate(raw_lines):
        if apportion:
            if idx == last_idx:
                line_total = total - running
            else:
                line_total = int(
                    (Decimal(raw_total) * total / gross).quantize(
                        Decimal('1'), rounding=ROUND_HALF_UP)
                )
        else:
            line_total = raw_total
        running += line_total
        ikpu = getattr(item.product, 'ikpu_code', '') or ''
        items.append({
            'name': (item.product.name if item.product else 'Item')[:63],
            'ikpu': ikpu,
            'package_code': '',
            'price': line_total,
            'quantity': item.quantity,
            'vat_percent': vat_percent,
            'vat': _vat_portion(line_total, vat_percent),
        })

    # Tender split. The FISCAL partition is CASH vs ALL-NON-CASH — it is NOT the
    # reporting partition {cash, card, payme}: Payme is electronic money and must be
    # declared under received_card, never as physical cash.
    #
    # Previously this read `is_cash = payment_method == 'CASH'`, so a MIXED cash+card
    # sale declared 100% CARD to the OFD. And received_cash must never come from the
    # raw CASH OrderPayment line — that is the TENDERED amount (it includes the
    # customer's change), so received_cash + received_card would exceed `total` and
    # the OFD rejects the receipt. Derive card, then subtract IN TIYIN so the two
    # fields reconcile exactly to `total` under ROUND_HALF_UP.
    from base.services.tender import order_tender_split
    split, _ = order_tender_split(order)
    if split['unknown']:
        # e.g. a MIXED order with no payment lines. Guessing would misdeclare to the
        # tax authority in one direction or the other; let the retry queue hold it.
        raise ValueError(
            f'cannot fiscalize order {order.id}: {split["unknown"]} of revenue has no '
            f'determinable tender (payment_method={order.payment_method!r}, no payment lines)'
        )
    received_card = _to_tiyin(split['card'] + split['payme'])
    received_card = max(0, min(received_card, total))
    received_cash = total - received_card

    return {
        'tin': tenant.get('tin', ''),
        'receipt_type': receipt_type,
        'order_id': order.id,
        'order_number': order.display_id,
        'received_cash': received_cash,
        'received_card': received_card,
        'total': total,
        'items': items,
    }


def missing_ikpu_products(order):
    """Line items whose product has no IKPU code — these will be rejected by a
    live OFD. Surfaced so the operator can fix the catalog before going live."""
    missing = []
    for item in order.items.select_related('product').all():
        if not (getattr(item.product, 'ikpu_code', '') or ''):
            missing.append(item.product.name if item.product else f'product {item.product_id}')
    return missing
