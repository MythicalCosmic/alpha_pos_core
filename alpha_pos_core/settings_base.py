import json
import os
import warnings
from pathlib import Path

BASE_DIR = Path(os.environ.get('ALPHA_POS_BASE_DIR') or Path.cwd())

# Writable state directory for logs, media, collected static files, and the
# SQLite fallback used by standalone core tooling.
DATA_DIR = Path(os.environ.get('ALPHA_POS_DATA_DIR') or BASE_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEBUG = os.environ.get('DEBUG', 'False').lower() in ('true', '1', 'yes')

# No fallback on purpose: an unset key must hit the guard below, not silently
# boot with a publicly-known constant (which would sign admin cookies + QR-order
# HMAC tokens). The desktop build generates a strong per-install key.
_DEV_SECRET_KEY = 'django-insecure-dev-only-key-do-not-use-in-production'
SECRET_KEY = os.environ.get('SECRET_KEY', '')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = _DEV_SECRET_KEY
    else:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured(
            "SECRET_KEY environment variable must be set when DEBUG is False."
        )

# Single fail-loud boot guard consolidating the DEBUG fail-open controls
# (LICENSE_DEV_BYPASS, ALLOWED_HOSTS=*, CORS_ALLOW_ALL, unbound branch tokens —
# all gated on DEBUG below). A production boot (DEBUG=False) must NOT be running
# with the publicly-known dev SECRET_KEY: if it is, either DEBUG was meant to be
# True (so the dev conveniences would silently stay off) or a real key is
# missing — both are misconfigurations that should crash the boot, not fail open.
if not DEBUG and SECRET_KEY == _DEV_SECRET_KEY:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        "Refusing to boot with the dev SECRET_KEY while DEBUG is False: set a "
        "real SECRET_KEY (production) or DEBUG=True (development)."
    )

# Dedicated signing key for public QR self-order table tokens (printed once on
# table stickers). notifications/services/qr_order_service.py reads this and falls
# back to SECRET_KEY when blank. Set a STABLE value (e.g. via config_store on the
# desktop) so rotating SECRET_KEY — or a self-update that regenerates it — does NOT
# invalidate every already-printed sticker. Blank by default = signed with
# SECRET_KEY (the historic behaviour; a factory reset then correctly kills old
# stickers for resale).
QR_SIGNING_KEY = os.environ.get('QR_SIGNING_KEY', '')

if DEBUG:
    ALLOWED_HOSTS = ['*']
else:
    ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')


# Application definition

# --- Edition-aware app sets (the split) ------------------------------------
# The SHARED spine is installed on BOTH editions (their tables must exist on both
# because base/services/sync/config.py:MODEL_MAP resolves every model on each
# pull). Each edition assembles INSTALLED_APPS via build_installed_apps([...]).
# Rule of the split: trim urls.py per edition, NEVER trim this list.
DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]
THIRD_PARTY_APPS = ['corsheaders', 'channels']
SHARED_APPS = ['base', 'stock', 'hr', 'discounts', 'notifications', 'fiscalization', 'cashbox', 'core.realtime']
# Licensing stays last; its kill-switch middleware is required on both editions.
LICENSING_APPS = ['licensing']


def build_installed_apps(edition_apps):
    # Shared spine + this edition's one-sided apps (server: admins; local: customers, waiters).
    return DJANGO_APPS + THIRD_PARTY_APPS + SHARED_APPS + list(edition_apps) + LICENSING_APPS


# Core's own manage.py / fallback installs the shared spine only; editions override.
INSTALLED_APPS = build_installed_apps([])

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    # License kill switch sits as early as possible while still being
    # AFTER corsheaders, so a 503 response carries CORS headers (the
    # Electron renderer would otherwise see only a CORS error and not
    # the license_inactive payload). Position-asserted at boot in
    # licensing/apps.py — moving this line will fail `manage.py check`.
    'licensing.middleware.LicenseEnforcementMiddleware',
    'django.middleware.security.SecurityMiddleware',
    # Serve collected admin assets without a separate static-file server.
    # WhiteNoise requires this position after SecurityMiddleware.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    # A custom session cookie and Bearer token may coexist in browser/Electron
    # requests. Refuse ambiguous credentials and require an explicit logout
    # before /auth-login changes the browser to a different custom User.
    'base.middlewares.login_transition_guard.LoginTransitionGuardMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'base.middlewares.force_json_middleware.JSONOnlyMiddleware'
]

# Trusted-LAN appliance mode: the desktop exposes the POS to the whole network,
# so requests come from arbitrary device IPs/origins. OPEN_LAN drops CSRF
# origin/host enforcement and opens CORS to every origin so nothing is left
# out. Auth (sessions/tokens) and the licensing kill-switch still apply. The
# desktop turns this on by default; server deployments leave it off.
OPEN_LAN = os.environ.get('OPEN_LAN', 'False').strip().lower() in ('1', 'true', 'yes', 'on')
if OPEN_LAN:
    _csrf_i = MIDDLEWARE.index('django.middleware.csrf.CsrfViewMiddleware')
    MIDDLEWARE.insert(_csrf_i, 'base.middlewares.disable_csrf.DisableCSRFMiddleware')

ROOT_URLCONF = os.environ.get('ROOT_URLCONF', 'alpha_pos_core.urls')

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = os.environ.get('WSGI_APPLICATION', 'config.wsgi.application')
ASGI_APPLICATION = os.environ.get('ASGI_APPLICATION', 'alpha_pos_core.asgi.application')


if os.environ.get('DB_ENGINE'):
    DATABASES = {
        'default': {
            'ENGINE': os.environ['DB_ENGINE'],
            'NAME': os.environ.get('DB_NAME', 'alpha_pos'),
            'USER': os.environ.get('DB_USER', 'alpha_pos'),
            'PASSWORD': os.environ.get('DB_PASSWORD', ''),
            'HOST': os.environ.get('DB_HOST', 'db'),
            'PORT': os.environ.get('DB_PORT', '5432'),
            # Bound the connect wait so a momentarily-busy embedded Postgres
            # (just-launched / mid-checkpoint / brief stall) fails fast with a
            # clear error instead of hanging a request indefinitely, and recycle
            # any dead/stale handle before reuse rather than erroring on it.
            'OPTIONS': {'connect_timeout': int(os.environ.get('DB_CONNECT_TIMEOUT', '10'))},
            'CONN_HEALTH_CHECKS': True,
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': DATA_DIR / 'db.sqlite3',
            # SQLite has no row-level locking, so every select_for_update() in
            # the codebase (stock levels, cash register, display-id counter,
            # orders) is a silent no-op here. The only real serialization is
            # SQLite's single-writer database lock. Make that lock usable:
            #   - WAL lets readers run concurrently with the one writer.
            #   - busy_timeout makes concurrent writers wait-and-retry instead
            #     of immediately raising "database is locked".
            #   - IMMEDIATE takes the write lock at BEGIN so atomic() blocks
            #     that read-then-write don't deadlock against each other.
            # This is correctness, not tuning: without it concurrent sales can
            # lose updates / oversell on the documented single-PC deployment.
            'OPTIONS': {
                'timeout': 20,
                'init_command': (
                    'PRAGMA journal_mode=WAL;'
                    'PRAGMA synchronous=NORMAL;'
                    'PRAGMA foreign_keys=ON;'
                ),
                'transaction_mode': 'IMMEDIATE',
            },
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


LANGUAGE_CODE = 'en-us'

# Wall-clock-sensitive features (attendance "today", shift boundaries,
# stamp accrual) compute against the local operator timezone, not UTC.
# Stored datetimes remain UTC under USE_TZ; only the display / localdate
# conversion changes. Override with TZ env var when deploying outside
# Uzbekistan.
TIME_ZONE = os.environ.get('TZ', 'Asia/Tashkent')

USE_I18N = True

USE_TZ = True


STATIC_URL = 'static/'
STATIC_ROOT = DATA_DIR / 'staticfiles'

# WhiteNoise compresses and serves STATIC_ROOT through the application server.
# The non-manifest backend avoids hard failures on missing asset references.
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage',
    },
}

# Private media (HR documents). Files are NOT served by Django's static
# file machinery — they're streamed only via auth-gated download views.
MEDIA_ROOT = os.environ.get('MEDIA_ROOT', str(DATA_DIR / 'private_media'))
MEDIA_URL = '/private-media/'  # not actually served; placeholder for FileField.url

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Cap upload sizes so a malicious / mistaken multi-GB upload can't OOM
# a worker. HR documents are the only file-upload surface; 10 MB is
# generous for ID scans / PDFs without leaving room for abuse.
DATA_UPLOAD_MAX_MEMORY_SIZE = int(
    os.environ.get('DATA_UPLOAD_MAX_MEMORY_SIZE', 10 * 1024 * 1024)
)
FILE_UPLOAD_MAX_MEMORY_SIZE = int(
    os.environ.get('FILE_UPLOAD_MAX_MEMORY_SIZE', 10 * 1024 * 1024)
)
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000

BRANCH_ID = os.environ.get('BRANCH_ID', 'main')
# Optional compatibility target for a genuinely single-branch cloud. A cloud
# process must never stamp branch-scoped creates with its own node id (usually
# ``cloud``), because no terminal can receive those rows. Leave empty in a
# multi-branch deployment and require every cloud CRUD call to pass branch_id.
CLOUD_DEFAULT_TARGET_BRANCH_ID = os.environ.get(
    'CLOUD_DEFAULT_TARGET_BRANCH_ID', '',
).strip()
# Per-install till identity (minted by the desktop at first run). Lets the cloud
# distinguish multiple tills even when they share one branch token — drives the
# POS presence registry (base/services/presence.py) that smartfood auto-dispatch
# reads. Empty on the cloud / unset installs.
DEVICE_ID = os.environ.get('DEVICE_ID', '')
# Whether login refuses a user whose branch_id differs from this instance's
# BRANCH_ID. OFF by default: users synced from the cloud (branch_id=cloud) would
# otherwise be locked out of a desktop branch. Operators running true isolated
# multi-branch can set ENFORCE_BRANCH_LOGIN=True to re-enable the guard.
ENFORCE_BRANCH_LOGIN = os.environ.get('ENFORCE_BRANCH_LOGIN', 'False').strip().lower() in ('1', 'true', 'yes', 'on')
DEPLOYMENT_MODE = os.environ.get('DEPLOYMENT_MODE', 'local')
SYNC_ON_SAVE = False

# AI assistant + demand forecast (base/services/llm.py). The operator picks the
# provider and pastes the matching key in the desktop panel (AI section) / env.
AI_PROVIDER = os.environ.get('AI_PROVIDER', 'openai')
# Claude (Anthropic). Current Sonnet default; also claude-sonnet-4-5 / claude-opus-4-8.
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_MODEL = os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-6')
# Gemini (Google).
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')
# OpenAI. GPT-5-class models use max_completion_tokens (handled in base.services.llm).
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-5.6-luna')
# Luna is used for cost-sensitive database Q&A.  Pin a deliberate low effort
# instead of inheriting GPT-5.6's medium default, which would add reasoning
# latency/tokens on routine sales and stock lookups.
OPENAI_REASONING_EFFORT = os.environ.get('OPENAI_REASONING_EFFORT', 'low')
# Provider calls are synchronous, so both an individual network operation and
# the complete multi-tool turn need explicit ceilings.  The old implementation
# documented LLM_TIMEOUT_SECONDS but never bound it from the environment, which
# left production permanently stuck at a 30-second read timeout.
LLM_CONNECT_TIMEOUT_SECONDS = float(
    os.environ.get('LLM_CONNECT_TIMEOUT_SECONDS', '10')
)
LLM_READ_TIMEOUT_SECONDS = float(
    os.environ.get(
        'LLM_READ_TIMEOUT_SECONDS',
        os.environ.get('LLM_TIMEOUT_SECONDS', '45'),
    )
)
AI_REQUEST_DEADLINE_SECONDS = float(
    os.environ.get('AI_REQUEST_DEADLINE_SECONDS', '150')
)
AI_MAX_TOOL_ITERATIONS = int(os.environ.get('AI_MAX_TOOL_ITERATIONS', '8'))
# Optional ordered backups for deployments whose operator has explicitly
# approved sending AI prompts to more than one vendor.  Keep this opt-in: the
# prompts can contain restaurant sales, staff, stock, and customer context, so
# merely configuring a spare API key must not authorize cross-provider egress.
AI_FALLBACK_PROVIDERS = os.environ.get(
    'AI_FALLBACK_PROVIDERS',
    '',
)
# Sampling controls for providers/models that support them. Reasoning models
# omit temperature because those APIs reject it.
OPENAI_SEED = int(os.environ.get('OPENAI_SEED', '7'))
AI_TEMPERATURE = float(os.environ.get('AI_TEMPERATURE', '0'))

# Secret token Telegram includes as X-Telegram-Bot-Api-Secret-Token on every
# webhook call, set when registering the webhook URL via setWebhook. Without
# it the webhook returns 503 (intentional — better than accepting spoofed
# updates). Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
TELEGRAM_WEBHOOK_SECRET = os.environ.get('TELEGRAM_WEBHOOK_SECRET', '')

# --- Customer-facing Telegram bot (SERVER edition) — a SEPARATE bot/token from the
# staff notifications bot. Stripped to: greet in Uzbek + a button that opens the
# ordering web app. See notifications/services/customer_bot.py.
CUSTOMER_BOT_TOKEN = os.environ.get('CUSTOMER_BOT_TOKEN', '')
CUSTOMER_WEBHOOK_SECRET = os.environ.get('CUSTOMER_WEBHOOK_SECRET', '')
CUSTOMER_WEBAPP_URL = os.environ.get('CUSTOMER_WEBAPP_URL', 'https://example.com')

REDIS_URL = os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0')
USE_DUMMY_CACHE = os.environ.get(
    'USE_DUMMY_CACHE', ''
).lower() in ('true', '1', 'yes')

if os.environ.get('USE_REDIS', '').lower() in ('true', '1', 'yes'):
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': REDIS_URL,
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            },
            'KEY_PREFIX': 'alpha_pos',
            'TIMEOUT': 300,
        }
    }
elif USE_DUMMY_CACHE:
    if not DEBUG:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured(
            'USE_DUMMY_CACHE is test-only and cannot be enabled when DEBUG=False.'
        )
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.dummy.DummyCache',
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'alpha-pos-cache',
            'TIMEOUT': 300,
        }
}

# LocMemCache is per process, so authentication limits are multiplied by the
# number of web workers. Cloud deployments should use Redis.
_default_web_concurrency = 3 if DEPLOYMENT_MODE == 'cloud' else 1
try:
    WEB_CONCURRENCY = int(os.environ.get(
        'WEB_CONCURRENCY', str(_default_web_concurrency),
    ))
except ValueError:
    WEB_CONCURRENCY = _default_web_concurrency
if (not DEBUG
        and WEB_CONCURRENCY > 1
        and CACHES['default']['BACKEND'].endswith('locmem.LocMemCache')):
    warnings.warn(
        f'Rate limiting uses the per-process LocMemCache with '
        f'{WEB_CONCURRENCY} Uvicorn workers: auth throttles are effectively '
        f'multiplied by the worker count. Set USE_REDIS=true for shared, '
        f'correct rate limiting.',
        RuntimeWarning,
    )

SESSION_CACHE_TTL = 300

SYNC_ENABLED = os.environ.get('SYNC_ENABLED', 'False').lower() in ('true', '1', 'yes')
CLOUD_SYNC_URL = os.environ.get('CLOUD_SYNC_URL', '')
CLOUD_SYNC_TOKEN = os.environ.get('CLOUD_SYNC_TOKEN', '')
SYNC_INTERVAL = 30
SYNC_RETRY_INTERVAL = 60
SYNC_TIMEOUT = 30
SYNC_MAX_RETRIES = 5
SYNC_BATCH_SIZE = 500
# After this many failed delivery attempts, a queued record is dead-lettered:
# it stays in the table (visible via the queue/status endpoints) but is no
# longer re-sent every cycle, so a permanently-rejected row can't spin forever.
SYNC_MAX_QUEUE_ATTEMPTS = 25
# Comma-separated branch tokens (legacy, unbound) and an allowlist of branch
# ids the cloud will accept from them.
ALLOWED_BRANCH_TOKENS = [
    t.strip() for t in os.environ.get('ALLOWED_BRANCH_TOKENS', '').split(',') if t.strip()
]
ALLOWED_BRANCH_IDS = [
    b.strip() for b in os.environ.get('ALLOWED_BRANCH_IDS', '').split(',') if b.strip()
]
# Bind sync tokens to a specific branch so X-Branch-ID cannot be spoofed.
# Format (env BRANCH_TOKEN_MAP): JSON {"branch_token_string": "branch_id"}.
# When set, this takes precedence over ALLOWED_BRANCH_TOKENS (no per-branch bind).
try:
    BRANCH_TOKEN_MAP = json.loads(os.environ.get('BRANCH_TOKEN_MAP', '') or '{}')
    if not isinstance(BRANCH_TOKEN_MAP, dict):
        BRANCH_TOKEN_MAP = {}
except (ValueError, TypeError):
    BRANCH_TOKEN_MAP = {}
SYNC_PULL_ENABLED = True
# When True, the branch refuses a plaintext http:// CLOUD_SYNC_URL (the token
# and password hashes would traverse it unencrypted). Off by default so LAN
# deployments keep working; a one-time warning is logged on http either way.
SYNC_REQUIRE_HTTPS = os.environ.get('SYNC_REQUIRE_HTTPS', 'False').lower() in ('true', '1', 'yes')

# Gates the sync management endpoints (status / trigger / queue / report …).
# Production must set this — when DEBUG is off and the token is empty the
# endpoints refuse to serve, since they expose internal queue state and can
# trigger full pushes. DEBUG keeps them open for local development.
SYNC_MANAGEMENT_TOKEN = os.environ.get('SYNC_MANAGEMENT_TOKEN', '')

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True

# Origins Django trusts for CSRF when admin/browser POSTs arrive over HTTPS
# through a reverse proxy (it checks Origin/Referer against this list). Set e.g.
# CSRF_TRUSTED_ORIGINS=https://pos.1-2-3-4.nip.io for an IP/nip.io deployment.
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]

# Security settings for production
if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'same-origin'
    X_FRAME_OPTIONS = 'DENY'
    # Only enable SSL redirect when explicitly opted in via env so reverse-proxy
    # setups (the typical deployment) don't end up with a redirect loop.
    SECURE_SSL_REDIRECT = os.environ.get(
        'SECURE_SSL_REDIRECT', 'False'
    ).lower() in ('true', '1', 'yes')
    # Trust X-Forwarded-Proto from a known-good reverse proxy when terminating
    # TLS at the proxy. Configure only when behind such a proxy.
    if os.environ.get('TRUST_FORWARDED_PROTO', '').lower() in ('true', '1', 'yes'):
        SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# OPEN_LAN is the desktop/LAN appliance, which serves plain HTTP — so secure-only
# cookies are NEVER stored by the browser and a correct login silently bounces
# back to the login page (the session cookie is dropped). Allow cookies over
# HTTP and disable HSTS/SSL-redirect in this mode. Runs after the production
# block above so it wins regardless of DEBUG.
if OPEN_LAN:
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    SECURE_HSTS_SECONDS = 0
    SECURE_SSL_REDIRECT = False

# X-Forwarded-For trust toggle, separate from the proto toggle so an operator
# can opt in to one without the other. Off by default — without a proxy in
# front of the app, the header is attacker-controlled and would break IP-based
# rate limiting and audit attribution.
TRUST_FORWARDED_FOR = os.environ.get(
    'TRUST_FORWARDED_FOR', ''
).lower() in ('true', '1', 'yes')

# Pagination limits
MAX_PER_PAGE = 100

# Logging — file rotation in prod, console in dev. Override the log directory
# via LOG_DIR env if you want logs outside the project root.
LOG_DIR = os.environ.get('LOG_DIR', str(DATA_DIR / 'logs'))
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()

if not DEBUG:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        # If the configured directory isn't writable, fall back to console-only
        # so the process still boots; an operator can fix LOG_DIR later.
        LOG_DIR = None

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} {levelname} {name} [{process:d}] {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {name}: {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simple' if DEBUG else 'verbose',
            'level': LOG_LEVEL,
        },
        **(
            {
                'app_file': {
                    'class': 'logging.handlers.RotatingFileHandler',
                    'filename': os.path.join(LOG_DIR, 'app.log'),
                    'maxBytes': 10 * 1024 * 1024,
                    'backupCount': 5,
                    'formatter': 'verbose',
                    'level': LOG_LEVEL,
                },
                'error_file': {
                    'class': 'logging.handlers.RotatingFileHandler',
                    'filename': os.path.join(LOG_DIR, 'error.log'),
                    'maxBytes': 10 * 1024 * 1024,
                    'backupCount': 10,
                    'formatter': 'verbose',
                    'level': 'ERROR',
                },
            } if (not DEBUG and LOG_DIR) else {}
        ),
    },
    'loggers': {
        # Quiet down noisy third-party loggers; keep errors.
        'django.utils.autoreload': {'level': 'WARNING'},
        'urllib3': {'level': 'WARNING'},
        'requests': {'level': 'WARNING'},
    },
    'root': {
        'handlers': ['console'] + (['app_file', 'error_file'] if (not DEBUG and LOG_DIR) else []),
        'level': LOG_LEVEL,
    },
}

# Soliq fiscalization (Uzbek tax / OFD)
# Mode: off | mock | sandbox | live.
FISCALIZATION_MODE = os.environ.get('FISCALIZATION_MODE', 'off')
FISCAL_PROVIDER = os.environ.get('FISCAL_PROVIDER', 'mock')
# When false, an unavailable provider queues the receipt instead of blocking
# the sale.
FISCAL_BLOCK_ON_FAILURE = os.environ.get('FISCAL_BLOCK_ON_FAILURE', 'false').lower() in ('1', 'true', 'yes')
FISCAL_TIN = os.environ.get('FISCAL_TIN', '')
FISCAL_PROVIDER_URL = os.environ.get('FISCAL_PROVIDER_URL', '')
FISCAL_MERCHANT_ID = os.environ.get('FISCAL_MERCHANT_ID', '')
FISCAL_SECRET = os.environ.get('FISCAL_SECRET', '')
try:
    FISCAL_VAT_PERCENT = int(os.environ.get('FISCAL_VAT_PERCENT', '0') or '0')
except ValueError:
    FISCAL_VAT_PERCENT = 0

# Licensing / control plane
LICENSE_CONTROL_CENTER_URL = os.environ.get('LICENSE_CONTROL_CENTER_URL', '')
if (
    LICENSE_CONTROL_CENTER_URL
    and not DEBUG
    and not LICENSE_CONTROL_CENTER_URL.startswith('https://')
):
    # Plaintext control-center traffic is a license-bypass vector: a MITM can
    # rewrite the heartbeat response to keep returning ACTIVE forever. Refuse
    # to boot rather than silently relying on a downgraded channel.
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        "LICENSE_CONTROL_CENTER_URL must use https:// when DEBUG is False."
    )

# Fernet key used to encrypt the bearer license key at rest. Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Pin once per deployment; rotating this value invalidates the stored key.
LICENSE_FERNET_KEY = os.environ.get('LICENSE_FERNET_KEY', '')

LICENSE_HEARTBEAT_INTERVAL = int(os.environ.get('LICENSE_HEARTBEAT_INTERVAL', 300))

# Offline grace window before the kill switch fires.
LICENSE_GRACE_DAYS = int(os.environ.get('LICENSE_GRACE_DAYS', 7))

LICENSE_HTTP_TIMEOUT_S = int(os.environ.get('LICENSE_HTTP_TIMEOUT_S', 10))

# Heartbeats invalidate this snapshot after each update.
LICENSE_STATE_CACHE_TTL = int(os.environ.get('LICENSE_STATE_CACHE_TTL', 60))

LICENSE_BACKOFF_SCHEDULE_S = tuple(
    int(x) for x in os.environ.get(
        'LICENSE_BACKOFF_SCHEDULE_S', '300,900,3600',
    ).split(',') if x.strip()
) or (300, 900, 3600)

# This bypass must remain gated by DEBUG; enabling it in production would
# disable the licensing kill switch.
LICENSE_DEV_BYPASS = DEBUG and os.environ.get(
    'LICENSE_DEV_BYPASS', ''
).lower() in ('true', '1', 'yes')

# Cross-origin POS clients use an explicit production allowlist. Debug mode
# opens origins only when no allowlist is configured.
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get('CORS_ALLOWED_ORIGINS', '').split(',') if o.strip()
]
CORS_ALLOW_CREDENTIALS = bool(CORS_ALLOWED_ORIGINS)
if DEBUG and not CORS_ALLOWED_ORIGINS:
    CORS_ALLOW_ALL_ORIGINS = True
    CORS_ALLOW_CREDENTIALS = False
# OPEN_LAN also changes CSRF/cookie behavior; CORS_ALLOW_ALL changes only CORS.
CORS_ALLOW_ALL = os.environ.get('CORS_ALLOW_ALL', 'False').strip().lower() in ('1', 'true', 'yes', 'on')
if OPEN_LAN or CORS_ALLOW_ALL:
    CORS_ALLOW_ALL_ORIGINS = True
    CORS_ALLOW_CREDENTIALS = False

# Browser write clients need this header for idempotent payments and orders.
from corsheaders.defaults import default_headers  # noqa: E402
CORS_ALLOW_HEADERS = (*default_headers, 'idempotency-key')
