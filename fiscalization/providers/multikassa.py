"""Fail-closed Multikassa placeholder pending an official provider contract."""

from fiscalization.providers.base import FiscalProvider, FiscalResult


class MultikassaProvider(FiscalProvider):
    name = 'multikassa'

    def _require_config(self):
        missing = [
            k for k in ('base_url', 'merchant_id', 'secret', 'tin')
            if not self.tenant.get(k)
        ]
        return missing

    def fiscalize(self, payload):
        missing = self._require_config()
        if missing:
            return FiscalResult.fail(
                f'multikassa not configured (missing: {", ".join(missing)})'
            )
        return FiscalResult.fail(
            'multikassa provider not yet wired — awaiting integration docs + '
            'credentials. Use FISCALIZATION_MODE=mock to test the full flow.'
        )
