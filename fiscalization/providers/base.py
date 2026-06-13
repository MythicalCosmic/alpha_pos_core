"""Provider abstraction. Every OFD / virtual-cash-register integration plugs in
behind this interface, so swapping Multikassa for Soliq-Servis (or adding a new
one) never touches the order flow.

A receipt payload is a provider-neutral dict built by
fiscalization.services.builder.build_receipt_payload():

    {
        'tin': '123456789',
        'receipt_type': 'SALE' | 'REFUND',
        'order_id': 42,
        'order_number': 'A-0042',
        'received_cash': 5000000,   # tiyin
        'received_card': 0,
        'total': 5000000,           # tiyin
        'items': [
            {
                'name': 'Lavash',
                'ikpu': '00803001001000000',  # SPIC / MXIK
                'package_code': '',
                'price': 2500000,   # tiyin, line total
                'quantity': 2,
                'vat_percent': 12,
                'vat': 267857,      # tiyin
            },
            ...
        ],
    }
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class FiscalResult:
    success: bool
    fiscal_sign: Optional[str] = None
    qr_url: Optional[str] = None
    fiscal_number: Optional[str] = None
    raw_response: Dict[str, Any] = field(default_factory=dict)
    error: str = ''

    @classmethod
    def ok(cls, fiscal_sign, qr_url, fiscal_number, raw_response=None):
        return cls(
            success=True, fiscal_sign=fiscal_sign, qr_url=qr_url,
            fiscal_number=fiscal_number, raw_response=raw_response or {},
        )

    @classmethod
    def fail(cls, error, raw_response=None):
        return cls(success=False, error=str(error)[:1000], raw_response=raw_response or {})


class FiscalProvider(ABC):
    name = 'base'

    def __init__(self, tenant: Dict[str, Any]):
        # Per-install fiscal identity + connection settings (TIN, base_url,
        # merchant_id, secret, sandbox flag). NEVER shared across tenants.
        self.tenant = tenant or {}

    @abstractmethod
    def fiscalize(self, payload: Dict[str, Any]) -> FiscalResult:
        """Register a SALE receipt and return its fiscal sign + QR."""

    def fiscalize_refund(self, payload: Dict[str, Any]) -> FiscalResult:
        """Register a REFUND. Default: not supported until a provider implements it."""
        return FiscalResult.fail(f'{self.name}: refund fiscalization not implemented')
