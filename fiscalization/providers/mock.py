"""Deterministic fake provider — the workhorse for dev, CI, demos and the
desktop control panel's "send mock data" button. No network, no credentials.

Given the same order it returns the same fiscal sign, so tests can assert on it.
The QR points at the real Soliq verifier host with a fake fiscal mark, so the UI
renders a believable receipt without contacting anyone.
"""
import hashlib

from fiscalization.providers.base import FiscalProvider, FiscalResult


class MockProvider(FiscalProvider):
    name = 'mock'

    def _sign(self, payload):
        seed = f"{payload.get('tin','')}|{payload.get('order_id','')}|" \
               f"{payload.get('receipt_type','')}|{payload.get('total','')}"
        digest = hashlib.sha256(seed.encode('utf-8')).hexdigest()
        # Soliq fiscal signs are 12-digit; derive a stable numeric one.
        return str(int(digest[:12], 16)).zfill(12)[:12]

    def _build(self, payload):
        sign = self._sign(payload)
        number = f"MOCK-{payload.get('order_id', '0')}"
        qr = (
            'https://ofd.soliq.uz/epi?t=MOCK000000000000'
            f'&r={number}&c={payload.get("total", 0)}&s={sign}'
        )
        return FiscalResult.ok(
            fiscal_sign=sign, qr_url=qr, fiscal_number=number,
            raw_response={'mock': True, 'echo': payload},
        )

    def fiscalize(self, payload):
        if not payload.get('items'):
            return FiscalResult.fail('mock: receipt has no items')
        return self._build(payload)

    def fiscalize_refund(self, payload):
        return self._build(payload)
