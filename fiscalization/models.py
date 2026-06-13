from django.db import models


class FiscalReceipt(models.Model):
    """The fiscal record for one order (or refund). One Order can have a SALE
    receipt and later a REFUND receipt. Local-authoritative: this row is the
    proof a sale was reported to Soliq, including the fiscal sign + QR the
    customer can verify on ofd.soliq.uz.
    """

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'        # created, not yet sent
        SENT = 'SENT', 'Sent'                  # sent, awaiting confirmation
        CONFIRMED = 'CONFIRMED', 'Confirmed'   # provider returned a fiscal sign
        FAILED = 'FAILED', 'Failed'            # error; eligible for retry
        SKIPPED = 'SKIPPED', 'Skipped'         # fiscalization disabled at the time

    class ReceiptType(models.TextChoices):
        SALE = 'SALE', 'Sale'
        REFUND = 'REFUND', 'Refund'

    order = models.ForeignKey(
        'base.Order', on_delete=models.CASCADE, related_name='fiscal_receipts',
        db_index=True,
    )
    receipt_type = models.CharField(
        max_length=10, choices=ReceiptType.choices, default=ReceiptType.SALE,
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True,
    )
    # Which provider + environment produced this row, so a mock/sandbox receipt
    # is never mistaken for a real one.
    provider = models.CharField(max_length=40, default='')
    mode = models.CharField(max_length=10, default='')

    # The fiscal proof returned by the OFD.
    fiscal_sign = models.CharField(max_length=64, null=True, blank=True)
    qr_url = models.TextField(null=True, blank=True)
    fiscal_number = models.CharField(max_length=64, null=True, blank=True)

    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    branch_id = models.CharField(max_length=64, default='', db_index=True)

    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    error = models.TextField(default='', blank=True)
    attempts = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    fiscalized_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['status', 'receipt_type']),
        ]
        constraints = [
            # One SALE and one REFUND receipt per order at most. Prevents a
            # double-tap on pay from fiscalizing the same sale twice.
            models.UniqueConstraint(
                fields=['order', 'receipt_type'],
                name='uniq_order_receipt_type',
            ),
        ]

    def __str__(self):
        return f'{self.receipt_type} #{self.order_id} [{self.status}]'
