from functools import wraps
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from base.helpers.response import ServiceResponse
from base.security.permissions import backoffice_required
from stock.services.purchase_invoices import json_numbers, queries
from stock.services.purchase_invoices.commands import DirectPurchaseInvoiceService


def safe_invoice_response(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except Exception:
            logging.getLogger(__name__).exception("Supplier invoice command failed")
            return _response(
                ServiceResponse.failure(
                    "INVOICE_OPERATION_FAILED",
                    "The operation could not be completed. Retry with the same idempotency key.",
                    500,
                )
            )

    return wrapped


def _response(result):
    body, status = result
    # Preserve the standard JSON response marker used by JSONOnlyMiddleware,
    # while encoding Decimal values as exact JSON numbers rather than strings.
    response = JsonResponse({}, status=status)
    response.content = json_numbers.dumps(body)
    return response


def _payload(request):
    try:
        return json_numbers.loads(request.body), None
    except json_numbers.InvalidNumbers as exc:
        return None, _response(ServiceResponse.validation_error(exc.errors))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None, _response(
            ServiceResponse.validation_error(
                {"body": ["Provide valid JSON using plain, finite numbers."]}
            )
        )


@require_GET
@backoffice_required
@safe_invoice_response
def receivable_items(request, supplier_id):
    return _response(
        queries.receivable_items(
            actor=request.user, supplier_id=supplier_id, params=request.GET
        )
    )


@require_GET
@backoffice_required
@safe_invoice_response
def purchase_invoices(request):
    return _response(queries.list_invoices(actor=request.user, params=request.GET))


@require_GET
@backoffice_required
@safe_invoice_response
def purchase_invoice_detail(request, invoice_id):
    return _response(queries.detail(actor=request.user, invoice_id=invoice_id))


@csrf_exempt
@require_POST
@backoffice_required
@safe_invoice_response
def receive(request):
    payload, error = _payload(request)
    if error is not None:
        return error
    return _response(
        DirectPurchaseInvoiceService.receive(
            actor=request.user,
            payload=payload,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
        )
    )


@csrf_exempt
@require_POST
@backoffice_required
@safe_invoice_response
def reverse(request, invoice_id):
    payload, error = _payload(request)
    if error is not None:
        return error
    return _response(
        DirectPurchaseInvoiceService.reverse(
            actor=request.user,
            invoice_id=invoice_id,
            payload=payload,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
        )
    )
