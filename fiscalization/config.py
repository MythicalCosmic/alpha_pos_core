"""Runtime configuration + on/off toggle for Soliq fiscalization.

Mirrors the pattern in base.services.sync.config.SyncConfig: a setting provides
the default, and a cache override lets the operator flip it at runtime (from the
desktop control panel or an admin endpoint) without a redeploy.

EVERYTHING here is PER-INSTALL. Each business runs its own deployment and enters
its OWN fiscal identity (TIN + provider credentials) — receipts are always
fiscalized under the selling business's tax registration, never the vendor's.
See docs/FISCALIZATION.md for why funnelling every customer through one ID is
illegal.
"""
from django.conf import settings

from base.services.sync.cache import safe_get, safe_set


CACHE_PREFIX = 'fiscal'

# Mode controls how much of the pipeline runs:
#   off     — fiscalization disabled entirely (no receipts created)
#   mock    — full pipeline against MockProvider (deterministic fake sign/QR,
#             no network); for local dev, CI and demos
#   sandbox — real provider, against its test endpoint + sandbox credentials
#   live    — real provider, production endpoint; real fiscal documents
MODES = ('off', 'mock', 'sandbox', 'live')


def _key(part):
    return f'{CACHE_PREFIX}:config:{part}'


class FiscalConfig:

    @classmethod
    def is_enabled(cls):
        """True unless mode is 'off'. A runtime cache override wins over the
        setting so the desktop panel can flip it instantly."""
        return cls.get_mode() != 'off'

    @classmethod
    def get_mode(cls):
        override = safe_get(_key('mode'))
        if override in MODES:
            return override
        mode = getattr(settings, 'FISCALIZATION_MODE', 'off')
        return mode if mode in MODES else 'off'

    @classmethod
    def set_mode(cls, mode):
        if mode not in MODES:
            raise ValueError(f'mode must be one of {MODES}')
        safe_set(_key('mode'), mode, None)

    @classmethod
    def get_provider_name(cls):
        # In mock mode the provider is always the mock, regardless of config,
        # so a half-configured live provider can't accidentally fire.
        if cls.get_mode() == 'mock':
            return 'mock'
        return getattr(settings, 'FISCAL_PROVIDER', 'mock')

    @classmethod
    def block_on_failure(cls):
        """Failure policy. Default False = serve-now: the sale completes and the
        receipt is queued for retry if the provider is unreachable (the right
        behaviour for a restaurant on a flaky link). True = refuse to finish the
        sale until Soliq confirms (strict compliance, stops service on outage)."""
        return bool(getattr(settings, 'FISCAL_BLOCK_ON_FAILURE', False))

    @classmethod
    def tenant(cls):
        """The per-install fiscal identity + provider connection settings.
        All env/settings-driven so each customer's deployment carries its own."""
        return {
            'tin': getattr(settings, 'FISCAL_TIN', ''),
            'provider': cls.get_provider_name(),
            'base_url': getattr(settings, 'FISCAL_PROVIDER_URL', ''),
            'merchant_id': getattr(settings, 'FISCAL_MERCHANT_ID', ''),
            'secret': getattr(settings, 'FISCAL_SECRET', ''),
            'vat_percent': getattr(settings, 'FISCAL_VAT_PERCENT', 0),
            'sandbox': cls.get_mode() == 'sandbox',
        }

    @classmethod
    def status(cls):
        """Non-secret snapshot for the control panel / status endpoint."""
        t = cls.tenant()
        return {
            'enabled': cls.is_enabled(),
            'mode': cls.get_mode(),
            'provider': cls.get_provider_name(),
            'tin_set': bool(t['tin']),
            'credentials_set': bool(t['merchant_id'] and t['secret']),
            'base_url_set': bool(t['base_url']),
            'block_on_failure': cls.block_on_failure(),
            'vat_percent': t['vat_percent'],
        }
