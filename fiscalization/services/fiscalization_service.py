"""Order → fiscal receipt orchestration.

Entry point is fiscalize_order(), called (safely) from the order pay flow. It
honours the runtime toggle, builds the payload, calls the configured provider,
and records the outcome on a FiscalReceipt row. Under the default serve-now
policy a provider failure is recorded as FAILED (not raised) so the sale still
completes and a retry sweep picks it up later.
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from base.helpers.response import ServiceResponse
from base.services.sync.evidence import emit_sync_evidence
from fiscalization.config import FiscalConfig
from fiscalization.models import FiscalReceipt
from fiscalization.providers import get_provider
from fiscalization.services.builder import build_receipt_payload

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8

# A receipt is flipped to SENT and committed BEFORE the provider call, then
# flipped to CONFIRMED/FAILED after. If the process dies in that window the row
# is stranded in SENT forever (the FAILED-only retry sweep never sees it). Treat
# a SENT row older than this as stranded and let retry_failed re-drive it.
SENT_STALE_SECONDS = 120


class FiscalizationService:

    @staticmethod
    def get_provider():
        cfg = FiscalConfig.tenant()
        return get_provider(cfg['provider'], cfg)

    @staticmethod
    def fiscalize_order(order_id, receipt_type=FiscalReceipt.ReceiptType.SALE):
        """Fiscalize one order. Idempotent: re-calling for an already-CONFIRMED
        receipt is a no-op. Returns (ServiceResponse, status)."""
        if not FiscalConfig.is_enabled():
            return ServiceResponse.success(
                data={'skipped': True, 'reason': 'fiscalization disabled'},
                message='Fiscalization disabled',
            )

        from base.models import Order
        order = Order.objects.filter(id=order_id).first()
        if not order:
            return ServiceResponse.not_found('Order not found')
        if (
            receipt_type == FiscalReceipt.ReceiptType.SALE
            and (not order.is_paid or order.paid_at is None)
        ):
            # The manual admin/management endpoint reaches this service too.
            # Never let it turn an unpaid kitchen ticket into an official sale
            # receipt; only the committed payment path may establish the paid
            # header (and its OrderPayment/CourierPayment evidence) first.
            return ServiceResponse.validation_error(
                errors={'order': 'Order payment has not been committed'},
                message='Cannot fiscalize an unpaid order',
            )

        mode = FiscalConfig.get_mode()
        cfg = FiscalConfig.tenant()

        with transaction.atomic():
            receipt, _ = FiscalReceipt.objects.select_for_update().get_or_create(
                order=order, receipt_type=receipt_type,
                defaults={
                    'status': FiscalReceipt.Status.PENDING,
                    'provider': cfg['provider'], 'mode': mode,
                    'amount': order.total_amount,
                    'branch_id': getattr(order, 'branch_id', '') or '',
                },
            )
            if receipt.status == FiscalReceipt.Status.CONFIRMED:
                return ServiceResponse.success(
                    data=FiscalizationService._serialize(receipt),
                    message='Already fiscalized',
                )

            payload = build_receipt_payload(order, cfg, receipt_type)
            receipt.request_payload = payload
            receipt.provider = cfg['provider']
            receipt.mode = mode
            receipt.attempts += 1
            receipt.status = FiscalReceipt.Status.SENT
            receipt.save()

        emit_sync_evidence(
            'fiscal_attempt_started',
            order_id=order.id,
            order_uuid=str(order.uuid),
            receipt_id=receipt.id,
            receipt_type=receipt_type,
            attempt=receipt.attempts,
            provider=receipt.provider,
            mode=receipt.mode,
            amount=str(receipt.amount),
            request_payload=payload,
            status=receipt.status,
        )

        # Provider call OUTSIDE the row lock — network I/O must not hold a DB
        # lock on the receipt row.
        provider = get_provider(cfg['provider'], cfg)
        try:
            if receipt_type == FiscalReceipt.ReceiptType.REFUND:
                result = provider.fiscalize_refund(payload)
            else:
                result = provider.fiscalize(payload)
        except Exception as exc:  # noqa: BLE001 — provider may raise anything
            logger.exception('fiscalize_order: provider raised')
            result = type('R', (), {'success': False, 'error': str(exc),
                                    'raw_response': {}, 'fiscal_sign': None,
                                    'qr_url': None, 'fiscal_number': None})()

        with transaction.atomic():
            receipt = FiscalReceipt.objects.select_for_update().get(pk=receipt.pk)
            receipt.response_payload = getattr(result, 'raw_response', {}) or {}
            if result.success:
                receipt.status = FiscalReceipt.Status.CONFIRMED
                receipt.fiscal_sign = result.fiscal_sign
                receipt.qr_url = result.qr_url
                receipt.fiscal_number = result.fiscal_number
                receipt.fiscalized_at = timezone.now()
                receipt.error = ''
            else:
                receipt.status = FiscalReceipt.Status.FAILED
                receipt.error = result.error or 'unknown provider error'
            receipt.save()

        emit_sync_evidence(
            'fiscal_attempt_completed',
            order_id=order.id,
            order_uuid=str(order.uuid),
            receipt_id=receipt.id,
            receipt_type=receipt_type,
            attempt=receipt.attempts,
            provider=receipt.provider,
            mode=receipt.mode,
            amount=str(receipt.amount),
            status=receipt.status,
            response_payload=receipt.response_payload,
            error=receipt.error,
            fiscal_sign=receipt.fiscal_sign,
            fiscal_number=receipt.fiscal_number,
            qr_url=receipt.qr_url,
            fiscalized_at=(
                receipt.fiscalized_at.isoformat() if receipt.fiscalized_at else None
            ),
        )

        if result.success:
            return ServiceResponse.success(
                data=FiscalizationService._serialize(receipt),
                message='Fiscalized',
            )
        # error() has no data slot; return the tuple shape directly so callers
        # still get the receipt snapshot alongside the failure.
        return ({'success': False, 'message': receipt.error,
                 'data': FiscalizationService._serialize(receipt)}, 400)

    @staticmethod
    def fiscalize_on_payment(order_id):
        """Hook for the order pay flow. NEVER raises.

        Return contract the pay-flow callers MUST honor:
          - Under serve-now policy (block_on_failure() False — the default) a
            provider failure is logged + the receipt is queued for retry and we
            still return the failure ServiceResponse, but the caller is free to
            ignore it and complete the sale (serve-now).
          - Under block_on_failure() True the returned value is a *failure*
            ServiceResponse whenever fiscalization did not succeed. Callers are
            REQUIRED to check `result['success']` and refuse to finish the sale
            when it is False; swallowing it defeats strict-compliance mode.
        """
        block = FiscalConfig.block_on_failure()
        try:
            result, _ = FiscalizationService.fiscalize_order(order_id)
        except Exception:
            logger.exception('fiscalize_on_payment failed (order=%s)', order_id)
            # Treat an unexpected crash as a failure too: surface it under
            # block-on-failure, otherwise it's swallowed by the serve-now caller.
            return ServiceResponse.error('fiscalization error (queued for retry)')

        # fiscalize_order returns either a success dict (ServiceResponse) or a
        # failure dict {'success': False, ...}. Pass both back verbatim so a
        # block-on-failure caller can read result['success']; a serve-now caller
        # ignores the failure and serves now exactly as before.
        if block and not result.get('success'):
            logger.warning(
                'fiscalize_on_payment: block_on_failure set and fiscalization '
                'failed (order=%s) — caller must refuse to finish the sale',
                order_id,
            )
        return result

    @staticmethod
    def _reap_stale_sent():
        """Rescue receipts stranded in SENT (process died between the pre-call
        SENT commit and the post-call CONFIRMED/FAILED commit).

        We have no provider receipt-id / idempotency token to ask the provider
        whether the prior send landed, so we cannot KNOW it failed. Conservative
        handling:
          - If a fiscal_sign is already present, the send actually succeeded and
            only the final status write was lost — promote it to CONFIRMED (no
            re-send).
          - Otherwise flip it to FAILED so the normal retry path re-drives it.
            This may re-send a receipt that secretly succeeded, but with no
            dedup field that is the safest available option (better a possible
            duplicate report than a silently unreported sale). Bounded by
            MAX_ATTEMPTS so it can't loop forever.
        Returns the number of rows flipped to FAILED (now eligible for retry)."""
        cutoff = timezone.now() - timedelta(seconds=SENT_STALE_SECONDS)
        stale = FiscalReceipt.objects.filter(
            status=FiscalReceipt.Status.SENT, updated_at__lt=cutoff,
        )
        flipped = 0
        for receipt in stale:
            with transaction.atomic():
                receipt = FiscalReceipt.objects.select_for_update().get(pk=receipt.pk)
                if receipt.status != FiscalReceipt.Status.SENT:
                    continue  # raced with the post-call commit; leave it alone
                if receipt.fiscal_sign:
                    receipt.status = FiscalReceipt.Status.CONFIRMED
                    if not receipt.fiscalized_at:
                        receipt.fiscalized_at = timezone.now()
                    receipt.error = ''
                    receipt.save()
                else:
                    receipt.status = FiscalReceipt.Status.FAILED
                    receipt.error = (
                        'stranded in SENT > %ss; re-queued for retry'
                        % SENT_STALE_SECONDS
                    )
                    receipt.save()
                    flipped += 1
        if flipped:
            logger.warning('retry_failed: reaped %d stale-SENT receipt(s)', flipped)
        return flipped

    @staticmethod
    def retry_failed(limit=100):
        """Re-attempt FAILED receipts under the retry cap. Run by the
        `fiscalize_retry` command / control-panel button / a periodic worker.

        First reaps stale-SENT rows (orphaned by a crash mid-send) into FAILED
        so they get swept in the same pass."""
        if not FiscalConfig.is_enabled():
            return {'retried': 0, 'confirmed': 0, 'still_failing': 0, 'skipped': True}
        FiscalizationService._reap_stale_sent()
        qs = FiscalReceipt.objects.filter(
            status=FiscalReceipt.Status.FAILED, attempts__lt=MAX_ATTEMPTS,
        ).order_by('updated_at')[:limit]
        retried = confirmed = still = 0
        for receipt in qs:
            retried += 1
            result, _ = FiscalizationService.fiscalize_order(
                receipt.order_id, receipt.receipt_type,
            )
            if result.get('success'):
                confirmed += 1
            else:
                still += 1
        # Surface receipts that have exhausted the retry cap: under serve-now the
        # sale already completed (cash taken), so a permanently-FAILED receipt is
        # an un-fiscalized sale that needs manual intervention. Log loudly rather
        # than let it fall silently out of the sweep.
        dead = FiscalReceipt.objects.filter(
            status=FiscalReceipt.Status.FAILED, attempts__gte=MAX_ATTEMPTS,
        ).count()
        if dead:
            logger.error(
                'fiscalize_retry: %d receipt(s) permanently FAILED at the %d-attempt '
                'cap — these sales are un-fiscalized and need manual intervention',
                dead, MAX_ATTEMPTS,
            )
        return {'retried': retried, 'confirmed': confirmed, 'still_failing': still,
                'dead_letter': dead}

    @staticmethod
    def stats():
        from django.db.models import Count
        rows = FiscalReceipt.objects.values('status').annotate(n=Count('id'))
        by_status = {r['status']: r['n'] for r in rows}
        return {
            'config': FiscalConfig.status(),
            'pending': by_status.get('PENDING', 0),
            'sent': by_status.get('SENT', 0),
            'confirmed': by_status.get('CONFIRMED', 0),
            'failed': by_status.get('FAILED', 0),
            'skipped': by_status.get('SKIPPED', 0),
            # Permanently-failed (hit the attempt cap) — needs operator attention.
            'dead_letter': FiscalReceipt.objects.filter(
                status=FiscalReceipt.Status.FAILED, attempts__gte=MAX_ATTEMPTS,
            ).count(),
        }

    @staticmethod
    def _serialize(receipt):
        return {
            'id': receipt.id,
            'order_id': receipt.order_id,
            'receipt_type': receipt.receipt_type,
            'status': receipt.status,
            'provider': receipt.provider,
            'mode': receipt.mode,
            'fiscal_sign': receipt.fiscal_sign,
            'qr_url': receipt.qr_url,
            'fiscal_number': receipt.fiscal_number,
            'amount': str(receipt.amount),
            'attempts': receipt.attempts,
            'error': receipt.error or None,
            'fiscalized_at': receipt.fiscalized_at.isoformat() if receipt.fiscalized_at else None,
        }
