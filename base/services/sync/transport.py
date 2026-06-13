import json
import logging
import requests
from base.services.sync.config import (
    get_cloud_url, get_cloud_token, get_branch_id,
    get_sync_timeout, get_sync_max_retries, get_sync_require_https,
)
from base.services.sync.encoder import SyncEncoder

logger = logging.getLogger(__name__)

_insecure_url_warned = False


def _auth_headers():
    return {
        'Authorization': f'Branch {get_cloud_token()}',
        'X-Branch-ID': get_branch_id(),
        'Content-Type': 'application/json',
    }


def _guard_url(url):
    """Return an error string if the URL must not be used, else None.

    Plaintext http:// carries the branch token and user password hashes in the
    clear. Warn once; refuse only when SYNC_REQUIRE_HTTPS is enabled so existing
    LAN deployments keep working until the operator opts in.
    """
    global _insecure_url_warned
    if not url.startswith('http://'):
        return None
    if get_sync_require_https():
        return 'CLOUD_SYNC_URL must use https:// (SYNC_REQUIRE_HTTPS is enabled)'
    if not _insecure_url_warned:
        logger.warning(
            'CLOUD_SYNC_URL uses plaintext http:// — the branch token and user '
            'password hashes traverse it unencrypted. Use https:// in '
            'production (set SYNC_REQUIRE_HTTPS=true to enforce).'
        )
        _insecure_url_warned = True
    return None


def check_health():
    url = get_cloud_url()
    if not url:
        return False
    if _guard_url(url):
        return False
    try:
        resp = requests.get(
            f'{url}/health',
            headers=_auth_headers(),
            timeout=5,
        )
        return resp.status_code == 200
    except Exception:
        return False


def send_batch(model_name, records, retry=True):
    url = get_cloud_url()
    if not url:
        return {'success': False, 'error': 'Cloud URL not configured'}
    guard = _guard_url(url)
    if guard:
        return {'success': False, 'error': guard}

    payload = json.dumps({
        'model': model_name,
        'branch_id': get_branch_id(),
        'records': records,
    }, cls=SyncEncoder)

    max_retries = get_sync_max_retries() if retry else 1
    timeout = get_sync_timeout()
    last_error = None

    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f'{url}/receive',
                headers=_auth_headers(),
                data=payload,
                timeout=timeout,
            )

            if resp.status_code == 200:
                data = resp.json()
                errors = data.get('errors', [])

                if errors and data.get('created', 0) == 0 and data.get('updated', 0) == 0:
                    return {
                        'success': False,
                        'error': f'Server rejected all records: {errors[0][:200]}',
                        'failed_uuids': data.get('failed_uuids', []),
                        'response': data,
                    }

                return {
                    'success': True,
                    'created': data.get('created', 0),
                    'updated': data.get('updated', 0),
                    'skipped': data.get('skipped', 0),
                    'errors': errors,
                    # Records the receiver could not apply. The pusher keeps these
                    # queued instead of purging them on this HTTP-200.
                    'failed_uuids': data.get('failed_uuids', []),
                    'response': data,
                }

            last_error = f'HTTP {resp.status_code}: {resp.text[:200]}'
            logger.warning(f'Sync attempt {attempt + 1}/{max_retries} failed: {last_error}')

        except requests.exceptions.Timeout:
            last_error = 'Request timeout'
            logger.warning(f'Sync attempt {attempt + 1}/{max_retries}: timeout')
        except requests.exceptions.ConnectionError:
            last_error = 'Connection failed'
            logger.warning(f'Sync attempt {attempt + 1}/{max_retries}: connection failed')
        except Exception as e:
            last_error = str(e)
            logger.error(f'Sync attempt {attempt + 1}/{max_retries}: {e}')

        if attempt < max_retries - 1:
            import time
            backoff = min(2 ** attempt, 30)
            time.sleep(backoff)

    return {'success': False, 'error': last_error}


def fetch_changes(since_timestamp=None):
    url = get_cloud_url()
    if not url:
        return {'success': False, 'error': 'Cloud URL not configured'}
    guard = _guard_url(url)
    if guard:
        return {'success': False, 'error': guard}

    params = {'branch_id': get_branch_id()}
    if since_timestamp:
        params['since'] = since_timestamp

    max_retries = get_sync_max_retries()
    timeout = get_sync_timeout()
    last_error = None

    for attempt in range(max_retries):
        try:
            resp = requests.get(
                f'{url}/changes',
                headers=_auth_headers(),
                params=params,
                timeout=timeout,
            )

            if resp.status_code == 200:
                data = resp.json()
                return {
                    'success': True,
                    'data': data.get('data', {}),
                    'server_timestamp': data.get('server_timestamp'),
                    # Surface pagination so the caller can page the rest of a
                    # large change set instead of silently dropping everything
                    # past the first page (which permanently loses data for a
                    # long-disconnected branch).
                    'has_more': data.get('has_more', False),
                    'next_since': data.get('next_since'),
                }

            last_error = f'HTTP {resp.status_code}: {resp.text[:200]}'
            logger.warning(f'Pull attempt {attempt + 1}/{max_retries} failed: {last_error}')

        except requests.exceptions.Timeout:
            last_error = 'Request timeout'
            logger.warning(f'Pull attempt {attempt + 1}/{max_retries}: timeout')
        except requests.exceptions.ConnectionError:
            last_error = 'Connection failed'
            logger.warning(f'Pull attempt {attempt + 1}/{max_retries}: connection failed')
        except Exception as e:
            last_error = str(e)
            logger.error(f'Pull attempt {attempt + 1}/{max_retries}: {e}')

        if attempt < max_retries - 1:
            import time
            backoff = min(2 ** attempt, 30)
            time.sleep(backoff)

    return {'success': False, 'error': last_error}
