import json
import logging
import hashlib
import uuid
import requests
from base.services.sync.config import (
    get_cloud_url, get_cloud_token, get_branch_id,
    get_sync_timeout, get_sync_max_retries, get_sync_require_https,
)
from base.services.sync.encoder import SyncEncoder
from base.services.sync.evidence import emit_sync_evidence

logger = logging.getLogger(__name__)

_insecure_url_warned = False


def _validated_ack_partition(data, records):
    """Return a complete v2 ACK partition or a fail-closed error."""
    if not isinstance(data, dict):
        return None, 'Server returned a non-object sync response'
    sent = [str(record.get('uuid') or '') for record in records]
    if any(not value for value in sent) or len(set(sent)) != len(sent):
        return None, 'Outbound batch UUIDs must be non-empty and unique'
    keys = (
        'acknowledged_uuids', 'retryable_uuids', 'rejected_uuids',
    )
    if data.get('ack_protocol_version') != 2 or not all(
        key in data for key in keys
    ):
        return None, (
            'Server response lacks the explicit acknowledgement partition; '
            'retaining the entire batch'
        )

    normalized = {}
    for key in keys:
        values = data.get(key)
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value for value in values
        ):
            return None, f'Server returned an invalid {key} partition'
        if len(set(values)) != len(values):
            return None, f'Server returned duplicate UUIDs in {key}'
        normalized[key] = values

    sent_set = set(sent)
    acknowledged = set(normalized['acknowledged_uuids'])
    retryable = set(normalized['retryable_uuids'])
    rejected = set(normalized['rejected_uuids'])
    if acknowledged & retryable or acknowledged & rejected or retryable & rejected:
        return None, 'Server acknowledgement partitions overlap'
    if acknowledged | retryable | rejected != sent_set:
        return None, (
            'Server acknowledgement partition does not exactly match the '
            'submitted UUIDs'
        )
    return normalized, None


def _auth_headers():
    headers = {
        'Authorization': f'Branch {get_cloud_token()}',
        'X-Branch-ID': get_branch_id(),
        # The server keeps v1 behavior safe during rolling upgrades and returns
        # canonical identity evidence only to clients which can apply it.
        'X-Sync-Ack-Protocol': '2',
        'Content-Type': 'application/json',
    }
    # Heartbeat presence: every sync request doubles as this till's "I'm online,
    # cashier X is active" ping (the cloud records it; auto-dispatch reads it).
    # Best-effort — never let a presence lookup block a sync.
    try:
        from base.services.presence import device_presence_headers
        headers.update(device_presence_headers())
    except Exception:
        pass
    return headers


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
    batch_id = str(uuid.uuid4())
    payload_sha256 = hashlib.sha256(payload.encode('utf-8')).hexdigest()

    # A bad SYNC_MAX_RETRIES=0 setting must not turn sync into a silent no-op
    # that reports error=None without issuing even one request.
    max_retries = max(1, get_sync_max_retries()) if retry else 1
    timeout = get_sync_timeout()
    last_error = None

    for attempt in range(max_retries):
        attempt_number = attempt + 1
        emit_sync_evidence(
            'push_http_attempt',
            batch_id=batch_id,
            model_name=model_name,
            branch_id=get_branch_id(),
            attempt=attempt_number,
            max_attempts=max_retries,
            payload_sha256=payload_sha256,
            records=records,
        )
        try:
            resp = requests.post(
                f'{url}/receive',
                headers=_auth_headers(),
                data=payload,
                timeout=timeout,
            )

            if resp.status_code == 200:
                data = resp.json()
                errors = data.get('errors', []) if isinstance(data, dict) else []
                server_success = (
                    data.get('success') if isinstance(data, dict) else None
                )
                emit_sync_evidence(
                    'push_http_response',
                    batch_id=batch_id,
                    model_name=model_name,
                    attempt=attempt_number,
                    http_status=resp.status_code,
                    payload_sha256=payload_sha256,
                    response=data,
                )

                partition, partition_error = _validated_ack_partition(
                    data, records,
                )
                if server_success is False or partition_error:
                    return {
                        'success': False,
                        'error': (
                            f'Server rejected batch: {errors[0][:200]}'
                            if server_success is False and errors
                            else partition_error or 'Server rejected batch'
                        ),
                        'acknowledged_uuids': [],
                        'retryable_uuids': [
                            str(record.get('uuid')) for record in records
                        ],
                        'rejected_uuids': [],
                        'failed_uuids': [
                            str(record.get('uuid')) for record in records
                        ],
                        'ack_partition_valid': False,
                        'response': data,
                    }

                return {
                    'success': True,
                    'created': data.get('created', 0),
                    'updated': data.get('updated', 0),
                    'skipped': data.get('skipped', 0),
                    'errors': errors,
                    **partition,
                    'failed_uuids': [
                        *partition['retryable_uuids'],
                        *partition['rejected_uuids'],
                    ],
                    'ack_partition_valid': True,
                    'record_results': data.get('record_results', []),
                    'response': data,
                }

            last_error = f'HTTP {resp.status_code}: {resp.text[:200]}'
            emit_sync_evidence(
                'push_http_response',
                batch_id=batch_id,
                model_name=model_name,
                attempt=attempt_number,
                http_status=resp.status_code,
                payload_sha256=payload_sha256,
                error=last_error,
            )
            logger.warning(f'Sync attempt {attempt + 1}/{max_retries} failed: {last_error}')

        except requests.exceptions.Timeout:
            last_error = 'Request timeout'
            emit_sync_evidence(
                'push_http_error', batch_id=batch_id, model_name=model_name,
                attempt=attempt_number, payload_sha256=payload_sha256,
                error=last_error,
            )
            logger.warning(f'Sync attempt {attempt + 1}/{max_retries}: timeout')
        except requests.exceptions.ConnectionError:
            last_error = 'Connection failed'
            emit_sync_evidence(
                'push_http_error', batch_id=batch_id, model_name=model_name,
                attempt=attempt_number, payload_sha256=payload_sha256,
                error=last_error,
            )
            logger.warning(f'Sync attempt {attempt + 1}/{max_retries}: connection failed')
        except Exception as e:
            last_error = str(e)
            emit_sync_evidence(
                'push_http_error', batch_id=batch_id, model_name=model_name,
                attempt=attempt_number, payload_sha256=payload_sha256,
                error=last_error,
            )
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

    max_retries = max(1, get_sync_max_retries())
    timeout = get_sync_timeout()
    last_error = None
    pull_id = str(uuid.uuid4())

    for attempt in range(max_retries):
        attempt_number = attempt + 1
        emit_sync_evidence(
            'pull_http_attempt', pull_id=pull_id, attempt=attempt_number,
            max_attempts=max_retries, branch_id=get_branch_id(), params=params,
        )
        try:
            resp = requests.get(
                f'{url}/changes',
                headers=_auth_headers(),
                params=params,
                timeout=timeout,
            )

            if resp.status_code == 200:
                data = resp.json()
                emit_sync_evidence(
                    'pull_http_response', pull_id=pull_id,
                    attempt=attempt_number, http_status=resp.status_code,
                    response=data,
                )
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
            emit_sync_evidence(
                'pull_http_response', pull_id=pull_id,
                attempt=attempt_number, http_status=resp.status_code,
                error=last_error,
            )
            logger.warning(f'Pull attempt {attempt + 1}/{max_retries} failed: {last_error}')

        except requests.exceptions.Timeout:
            last_error = 'Request timeout'
            emit_sync_evidence(
                'pull_http_error', pull_id=pull_id, attempt=attempt_number,
                error=last_error,
            )
            logger.warning(f'Pull attempt {attempt + 1}/{max_retries}: timeout')
        except requests.exceptions.ConnectionError:
            last_error = 'Connection failed'
            emit_sync_evidence(
                'pull_http_error', pull_id=pull_id, attempt=attempt_number,
                error=last_error,
            )
            logger.warning(f'Pull attempt {attempt + 1}/{max_retries}: connection failed')
        except Exception as e:
            last_error = str(e)
            emit_sync_evidence(
                'pull_http_error', pull_id=pull_id, attempt=attempt_number,
                error=last_error,
            )
            logger.error(f'Pull attempt {attempt + 1}/{max_retries}: {e}')

        if attempt < max_retries - 1:
            import time
            backoff = min(2 ** attempt, 30)
            time.sleep(backoff)

    return {'success': False, 'error': last_error}
