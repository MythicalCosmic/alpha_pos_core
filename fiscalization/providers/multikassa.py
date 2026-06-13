"""Multikassa (virtual cash register) provider — SKELETON.

Contract is finalised once we have Multikassa's integration docs + sandbox
credentials (see docs/FISCALIZATION.md "What you do"). The request shape below
follows the common Uzbek OFD convention surfaced during research (CLICK/OFD):
per item a name, SPIC/IKPU code, Price + VAT in tiyin, quantity; auth via an
HMAC header; the response carries a fiscal sign + a qrCodeURL on ofd.soliq.uz.

Until creds + the exact endpoint are wired, this fails CLEANLY (so the
serve-now queue retries) rather than guessing and POSTing a wrong body to a real
tax endpoint. Flip FISCALIZATION_MODE to 'mock' for end-to-end testing now.
"""
import hashlib
import hmac
import logging

import requests

from fiscalization.providers.base import FiscalProvider, FiscalResult

logger = logging.getLogger(__name__)


class MultikassaProvider(FiscalProvider):
    name = 'multikassa'

    def _require_config(self):
        missing = [
            k for k in ('base_url', 'merchant_id', 'secret', 'tin')
            if not self.tenant.get(k)
        ]
        return missing

    def _auth_header(self, body_str, timestamp):
        # Placeholder HMAC scheme (Uzbek OFD APIs commonly use
        # "merchant_id:hash:timestamp"). Confirm the exact algorithm against
        # Multikassa's docs before going live.
        raw = f"{self.tenant['merchant_id']}{body_str}{timestamp}".encode('utf-8')
        digest = hmac.new(
            self.tenant['secret'].encode('utf-8'), raw, hashlib.sha1,
        ).hexdigest()
        return f"{self.tenant['merchant_id']}:{digest}:{timestamp}"

    def fiscalize(self, payload):
        missing = self._require_config()
        if missing:
            return FiscalResult.fail(
                f'multikassa not configured (missing: {", ".join(missing)})'
            )
        # Contract not finalised — refuse to POST a guessed body to a live tax
        # endpoint. Replace this block with the real request/response mapping
        # once the integration docs land. Marked NotImplemented so a misset
        # 'live' mode surfaces loudly instead of silently no-op'ing.
        return FiscalResult.fail(
            'multikassa provider not yet wired — awaiting integration docs + '
            'credentials. Use FISCALIZATION_MODE=mock to test the full flow.'
        )

    # Reference implementation kept for when the contract is confirmed:
    #
    # def _send(self, endpoint, payload):
    #     import json, time
    #     body = {
    #         'service_id': self.tenant['merchant_id'],
    #         'TIN': self.tenant['tin'],
    #         'received_cash': payload['received_cash'],
    #         'received_card': payload['received_card'],
    #         'Items': [
    #             {
    #                 'Name': it['name'][:63],
    #                 'SPIC': it['ikpu'],
    #                 'Price': it['price'],
    #                 'Amount': it['quantity'],
    #                 'VAT': it['vat'],
    #                 'VATPercent': it['vat_percent'],
    #                 'PackageCode': it.get('package_code', ''),
    #             }
    #             for it in payload['items']
    #         ],
    #     }
    #     body_str = json.dumps(body, separators=(',', ':'))
    #     ts = str(int(time.time()))
    #     resp = requests.post(
    #         f"{self.tenant['base_url'].rstrip('/')}/{endpoint}",
    #         data=body_str,
    #         headers={'Content-Type': 'application/json',
    #                  'Auth': self._auth_header(body_str, ts)},
    #         timeout=15,
    #     )
    #     data = resp.json()
    #     if data.get('error_code') not in (0, '0', None):
    #         return FiscalResult.fail(data.get('error_note', 'provider error'), data)
    #     return FiscalResult.ok(
    #         fiscal_sign=data.get('fiscalSign'),
    #         qr_url=data.get('qrCodeURL'),
    #         fiscal_number=str(data.get('terminalId') or data.get('paymentId') or ''),
    #         raw_response=data,
    #     )
