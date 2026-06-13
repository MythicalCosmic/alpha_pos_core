from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from base.helpers.request import parse_json_body
from base.helpers.response import json_response, ServiceResponse
from base.security.permissions import admin_required
from fiscalization.config import FiscalConfig
from fiscalization.models import FiscalReceipt
from fiscalization.providers import MockProvider
from fiscalization.services import FiscalizationService


@csrf_exempt
@require_GET
@admin_required
def status_view(request):
    return JsonResponse({'success': True, 'data': FiscalizationService.stats()})


@csrf_exempt
@require_POST
@admin_required
def set_mode_view(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    mode = (data.get('mode') or '').lower()
    try:
        FiscalConfig.set_mode(mode)
    except ValueError as exc:
        return json_response(ServiceResponse.error(str(exc)))
    return JsonResponse({'success': True, 'data': FiscalConfig.status()})


@csrf_exempt
@require_POST
@admin_required
def fiscalize_view(request, order_id):
    result, status = FiscalizationService.fiscalize_order(order_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def retry_view(request):
    return JsonResponse({'success': True, 'data': FiscalizationService.retry_failed()})


@csrf_exempt
@require_GET
@admin_required
def list_view(request):
    status = request.GET.get('status')
    qs = FiscalReceipt.objects.all().order_by('-id')
    if status:
        qs = qs.filter(status=status.upper())
    rows = [FiscalizationService._serialize(r) for r in qs[:200]]
    return JsonResponse({'success': True, 'data': {'receipts': rows}})


@csrf_exempt
@require_POST
@admin_required
def test_view(request):
    """Dry-run the pipeline against the MockProvider with a synthetic receipt —
    no order, no network. Powers the control panel's "test fiscalization"
    button so an operator can confirm the wiring before real sales."""
    payload = {
        'tin': FiscalConfig.tenant().get('tin', '') or '000000000',
        'receipt_type': 'SALE', 'order_id': 'TEST', 'order_number': 'TEST',
        'received_cash': 5000000, 'received_card': 0, 'total': 5000000,
        'items': [{
            'name': 'Test item', 'ikpu': '00000000000000000', 'package_code': '',
            'price': 5000000, 'quantity': 1, 'vat_percent': 0, 'vat': 0,
        }],
    }
    result = MockProvider(FiscalConfig.tenant()).fiscalize(payload)
    return JsonResponse({
        'success': result.success,
        'data': {
            'fiscal_sign': result.fiscal_sign,
            'qr_url': result.qr_url,
            'fiscal_number': result.fiscal_number,
        },
        'message': 'Mock fiscalization OK' if result.success else result.error,
    })
