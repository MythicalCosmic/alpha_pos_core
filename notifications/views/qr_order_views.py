"""Public QR self-order endpoints + admin token mint.

Public routes (no auth, signed token only):
  GET  /api/qr/menu/<token>/   — menu JSON for the resolved table
  POST /api/qr/order/<token>/  — create an OPEN HALL order at the table

Admin routes (ADMIN only):
  GET  /api/admins/qr/tables/<table_id>/token/  — mint/display the token

Rate-limited per IP to stop trivial flood attacks. Tokens are stable for
the lifetime of the table; an attacker who scans a sticker still can
only place orders at *that* table (which the staff sees and can
cancel), not anywhere else.
"""
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from base.helpers.request import parse_json_body
from base.models import Category, Product, Table
from base.security.permissions import admin_required
from base.security.rate_limit import rate_limit
from notifications.services import qr_order_service

logger = logging.getLogger(__name__)


_ERROR_MESSAGES = {
    'items_empty': 'Provide at least one item',
    'items_too_many': 'Too many items in one order',
    'items_invalid': 'Invalid items payload',
    'quantity_out_of_range': 'Quantity out of range',
    'product_not_found': 'One of the products does not exist',
}


def _resolve_or_404(token):
    table = qr_order_service.resolve_token(token)
    if not table:
        return None, JsonResponse(
            {'success': False, 'message': 'Invalid or expired QR token'},
            status=404,
        )
    return table, None


@require_GET
@rate_limit('qr_menu', max_attempts=60, window=60)
def menu_view(request, token):
    table, err = _resolve_or_404(token)
    if err:
        return err

    categories = list(
        Category.objects.active()
        .filter(status='ACTIVE').order_by('sort_order', 'name')
        .values('id', 'name', 'slug', 'parent_id')
    )
    products = list(
        Product.objects.filter(is_deleted=False)
        .order_by('category_id', 'name')
        .values('id', 'name', 'price', 'category_id', 'description')
    )
    # Decimals don't serialize to JSON by default in JsonResponse — coerce.
    for p in products:
        p['price'] = str(p['price'])

    return JsonResponse({
        'success': True,
        'data': {
            'table': {
                'id': table.id,
                'number': table.number,
                'place': table.place.name if table.place else None,
            },
            'categories': categories,
            'products': products,
        },
    })


@csrf_exempt
@require_POST
@rate_limit('qr_order', max_attempts=10, window=60)
def order_view(request, token):
    table, err = _resolve_or_404(token)
    if err:
        return err

    data, parse_err = parse_json_body(request)
    if parse_err:
        return JsonResponse(parse_err[0], status=parse_err[1])

    rows, err = qr_order_service.validate_items(data.get('items'))
    if err:
        return JsonResponse(
            {'success': False, 'message': _ERROR_MESSAGES.get(err, err)},
            status=422,
        )

    order = qr_order_service.create_qr_order(
        table, rows, customer_note=data.get('note'),
    )
    return JsonResponse({
        'success': True,
        'data': {
            'display_id': order.display_id,
            'total': str(order.total_amount),
            'items': len(rows),
        },
    })


@require_GET
@admin_required
def mint_token(request, table_id):
    try:
        table = Table.objects.get(id=table_id, is_active=True, is_deleted=False)
    except Table.DoesNotExist:
        return JsonResponse(
            {'success': False, 'message': 'Table not found'}, status=404,
        )
    token = qr_order_service.make_token(table)
    return JsonResponse({
        'success': True,
        'data': {
            'table_id': table.id,
            'table_number': table.number,
            'token': token,
            'menu_url_suffix': f'/api/qr/menu/{token}/',
            'order_url_suffix': f'/api/qr/order/{token}/',
        },
    })
