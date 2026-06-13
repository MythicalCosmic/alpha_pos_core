from fiscalization.providers.base import FiscalProvider, FiscalResult
from fiscalization.providers.mock import MockProvider
from fiscalization.providers.multikassa import MultikassaProvider


def get_provider(name, tenant):
    """Factory: resolve a provider name + per-install tenant config to an
    instance. Unknown names fall back to the mock so a misconfiguration can
    never silently hit a live tax endpoint."""
    providers = {
        'mock': MockProvider,
        'multikassa': MultikassaProvider,
    }
    cls = providers.get((name or 'mock').lower(), MockProvider)
    return cls(tenant)


__all__ = [
    'FiscalProvider', 'FiscalResult', 'MockProvider', 'MultikassaProvider',
    'get_provider',
]
