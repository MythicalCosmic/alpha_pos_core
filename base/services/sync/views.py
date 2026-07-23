import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.conf import settings
from base.helpers.request import safe_per_page


logger = logging.getLogger(__name__)


@csrf_exempt
@require_GET
def health(request):
    from base.services.sync.config import SyncConfig
    return JsonResponse({
        'status': 'ok',
        'mode': getattr(settings, 'DEPLOYMENT_MODE', 'unknown'),
        'sync_enabled': SyncConfig.is_enabled(),
    })


def _resolve_branch_token(token):
    # Prefer BRANCH_TOKEN_MAP ({token: branch_id}) which binds each token to a
    # single branch and lets us reject mismatched X-Branch-ID headers. Fall
    # back to the legacy ALLOWED_BRANCH_TOKENS list (no binding) if the map
    # isn't configured.
    from django.utils.crypto import constant_time_compare
    token_map = getattr(settings, 'BRANCH_TOKEN_MAP', None) or {}
    for known_token, bound_branch in token_map.items():
        if constant_time_compare(token, known_token):
            return bound_branch, True
    allowed_tokens = getattr(settings, 'ALLOWED_BRANCH_TOKENS', [])
    if allowed_tokens and any(constant_time_compare(token, t) for t in allowed_tokens):
        return None, True
    return None, False


def _management_authorized(request):
    # Management endpoints (status / trigger / queue / report …) expose internal
    # state and can trigger full pushes. The token is required unconditionally:
    # tying auth to DEBUG meant a deploy that booted with DEBUG=True (operator
    # error, env override) would expose unauthenticated control endpoints.
    # Local devs set SYNC_MANAGEMENT_TOKEN in their .env explicitly.
    from django.utils.crypto import constant_time_compare
    expected = getattr(settings, 'SYNC_MANAGEMENT_TOKEN', '') or ''
    if not expected:
        return False
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    prefix = 'Management '
    if not auth.startswith(prefix):
        return False
    return constant_time_compare(auth[len(prefix):], expected)


def _management_denied():
    return JsonResponse(
        {'error': 'Sync management endpoint requires Authorization: Management <token>'},
        status=401,
    )


def _authenticated_branch_scope(request):
    """Return ``(branch_id, error_response)`` for a branch sync credential.

    Close acknowledgement exposes financial integrity state, so it uses the
    same token-to-branch binding and production fail-closed rules as receive.
    Reusing the receive endpoint's token resolver and fail-closed rules keeps
    the two contracts aligned without exposing management credentials.
    """
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if not auth.startswith('Branch '):
        return None, JsonResponse({'error': 'Invalid authorization'}, status=401)

    bound_branch, ok = _resolve_branch_token(auth[7:])
    if not ok:
        return None, JsonResponse({'error': 'Invalid branch token'}, status=401)

    branch_id = request.META.get('HTTP_X_BRANCH_ID', 'unknown')
    if bound_branch is not None:
        if branch_id != bound_branch:
            return None, JsonResponse(
                {'error': f'X-Branch-ID does not match token (expected {bound_branch})'},
                status=403,
            )
        return bound_branch, None

    allowed_ids = getattr(settings, 'ALLOWED_BRANCH_IDS', None)
    if allowed_ids:
        if branch_id not in allowed_ids:
            return None, JsonResponse(
                {'error': 'X-Branch-ID is not in ALLOWED_BRANCH_IDS'},
                status=403,
            )
    elif not settings.DEBUG:
        return None, JsonResponse(
            {'error': 'Unbound branch tokens are not permitted in production; '
                      'configure BRANCH_TOKEN_MAP or ALLOWED_BRANCH_IDS'},
            status=403,
        )
    return branch_id, None


@csrf_exempt
@require_POST
def receive(request):
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if not auth.startswith('Branch ') and not auth.startswith('Cloud '):
        return JsonResponse({'error': 'Invalid authorization'}, status=401)

    bound_branch = None
    is_cloud = auth.startswith('Cloud ')
    if is_cloud:
        from django.utils.crypto import constant_time_compare
        token = auth[6:]
        expected = getattr(settings, 'CLOUD_SYNC_TOKEN', '')
        if not expected or not constant_time_compare(token, expected):
            return JsonResponse({'error': 'Invalid cloud token'}, status=401)
    elif auth.startswith('Branch '):
        token = auth[7:]
        bound_branch, ok = _resolve_branch_token(token)
        if not ok:
            return JsonResponse({'error': 'Invalid branch token'}, status=401)

    branch_id = request.META.get('HTTP_X_BRANCH_ID', 'unknown')

    if is_cloud:
        # ``Cloud`` is a legacy direct hub -> terminal credential.  It is not
        # valid on the hub itself: accepting it there would turn one shared
        # outbound secret into an unbound arbitrary-branch write credential.
        # Normal branch -> hub traffic uses a branch-bound token, and normal
        # hub -> terminal replication uses the authenticated /changes pull.
        node_mode = getattr(settings, 'DEPLOYMENT_MODE', '')
        local_branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
        if node_mode != 'local':
            return JsonResponse(
                {'error': 'Cloud receive credentials are valid only on local nodes'},
                status=403,
            )
        if not local_branch or branch_id != local_branch:
            return JsonResponse(
                {'error': 'X-Branch-ID must match this local node'},
                status=403,
            )
        branch_id = local_branch

    # If the token was bound to a specific branch, the caller MUST present an
    # X-Branch-ID equal to that bound branch. Previously a bound token also
    # accepted the literal 'unknown' (and a missing header defaults to
    # 'unknown'), which let any token holder forge records under the catch-all
    # 'unknown' branch — bypassing per-branch filtering. Reject 'unknown' and
    # any mismatch outright.
    if bound_branch is not None:
        if branch_id != bound_branch:
            return JsonResponse(
                {'error': f'X-Branch-ID does not match token (expected {bound_branch})'},
                status=403,
            )
        branch_id = bound_branch
    elif not is_cloud:
        # Legacy unbound ALLOWED_BRANCH_TOKENS path: the X-Branch-ID is fully
        # caller-controlled, so without binding any token holder could write as
        # any branch. Require an explicit ALLOWED_BRANCH_IDS allowlist; in
        # production, refuse entirely if neither BRANCH_TOKEN_MAP nor the
        # allowlist is configured (fail closed). The Cloud token is exempt — it
        # is the trusted hub and legitimately pushes records for any branch.
        allowed_ids = getattr(settings, 'ALLOWED_BRANCH_IDS', None)
        if allowed_ids:
            if branch_id not in allowed_ids:
                return JsonResponse(
                    {'error': 'X-Branch-ID is not in ALLOWED_BRANCH_IDS'},
                    status=403,
                )
        elif not settings.DEBUG:
            return JsonResponse(
                {'error': 'Unbound branch tokens are not permitted in production; '
                          'configure BRANCH_TOKEN_MAP or ALLOWED_BRANCH_IDS'},
                status=403,
            )

    # Parse the body directly (not via dict-only parse_json_body): the
    # documented batch format is a JSON array, which parse_json_body rejects
    # with a 400 before this handler ever sees it — making the list branch
    # below dead code and hard-400ing every array-format push.
    import json
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    if isinstance(data, list):
        if not data:
            return JsonResponse({'error': 'Empty records'}, status=400)
        if not isinstance(data[0], dict):
            return JsonResponse(
                {'error': 'Array items must be objects'}, status=400,
            )
        # Require an explicit model_name — defaulting to 'order' would write a
        # malformed array as Orders.
        model_name = data[0].get('model_name')
        if not model_name:
            return JsonResponse(
                {'error': 'Array format requires model_name on the first item'},
                status=400,
            )
        records = []
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                return JsonResponse(
                    {'error': f'Array item {index} must be an object'},
                    status=400,
                )
            if item.get('model_name') != model_name:
                return JsonResponse(
                    {'error': (
                        f'Array item {index} must declare model_name='
                        f'{model_name}'
                    )},
                    status=400,
                )
            record = item.get('data', item)
            if not isinstance(record, dict):
                return JsonResponse(
                    {'error': f'Array item {index} data must be an object'},
                    status=400,
                )
            records.append(record)
    elif isinstance(data, dict):
        model_name = data.get('model')
        records = data.get('records', [])
    else:
        return JsonResponse({'error': 'Expected JSON object or array'}, status=400)

    if not model_name or not records:
        return JsonResponse({'error': 'Missing model or records'}, status=400)
    if not isinstance(records, list):
        return JsonResponse({'error': 'records must be an array'}, status=400)
    record_uuids = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            return JsonResponse(
                {'error': f'records[{index}] must be an object'}, status=400,
            )
        record_uuid = str(record.get('uuid') or '')
        if not record_uuid:
            return JsonResponse(
                {'error': f'records[{index}] is missing uuid'}, status=400,
            )
        record_uuids.append(record_uuid)
    if len(set(record_uuids)) != len(record_uuids):
        return JsonResponse(
            {'error': 'record UUIDs must be unique within a batch'}, status=400,
        )

    # Heartbeat presence: this authenticated push proves the till (branch_id +
    # device) is online now; record its active cashier so smartfood auto-dispatch
    # can target a CONNECTED POS. Best-effort, no-op without the device header.
    from base.services.presence import mark_device_live
    mark_device_live(
        request.META.get('HTTP_X_DEVICE_ID', ''),
        branch_id,
        request.META.get('HTTP_X_ACTIVE_CASHIER', ''),
    )

    from base.services.sync.receiver import CloudReceiver
    try:
        client_ack_protocol = int(
            request.META.get('HTTP_X_SYNC_ACK_PROTOCOL') or 1
        )
    except (TypeError, ValueError):
        client_ack_protocol = 1
    if client_ack_protocol != 2:
        client_ack_protocol = 1
    result = CloudReceiver.receive_batch(
        model_name,
        branch_id,
        records,
        client_ack_protocol=client_ack_protocol,
    )

    return JsonResponse(result)


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def shift_close_ack(request):
    """Validate whether one terminal close is complete on the cloud.

    Both methods expose the same read-only operation: GET is useful for support
    inspection, while POST keeps the manifest identity out of access-log query
    strings for the desktop's normal polling path.
    """
    branch_id, denied = _authenticated_branch_scope(request)
    if denied is not None:
        return denied
    if getattr(settings, 'DEPLOYMENT_MODE', '') != 'cloud':
        return JsonResponse(
            {'error': 'Shift close acknowledgement is available only on the cloud'},
            status=403,
        )

    if request.method == 'GET':
        payload = request.GET
    else:
        import json
        try:
            payload = json.loads(request.body or b'{}')
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        if not isinstance(payload, dict):
            return JsonResponse({'error': 'Expected JSON object'}, status=400)

    raw_uuid = payload.get('shift_uuid')
    raw_version = payload.get('manifest_version')
    raw_digest = str(payload.get('manifest_digest') or '').strip().lower()
    try:
        from uuid import UUID
        shift_uuid = UUID(str(raw_uuid))
    except (TypeError, ValueError, AttributeError):
        return JsonResponse({'error': 'shift_uuid must be a valid UUID'}, status=400)
    try:
        manifest_version = int(raw_version)
    except (TypeError, ValueError):
        return JsonResponse({'error': 'manifest_version must be an integer'}, status=400)
    if manifest_version < 1:
        return JsonResponse({'error': 'manifest_version must be positive'}, status=400)
    if len(raw_digest) != 64 or any(
        char not in '0123456789abcdef' for char in raw_digest
    ):
        return JsonResponse(
            {'error': 'manifest_digest must be a 64-character SHA-256 hex digest'},
            status=400,
        )

    from core.shifts.service import shift_close_acknowledgement
    return JsonResponse(shift_close_acknowledgement(
        shift_uuid=shift_uuid,
        branch_id=branch_id,
        manifest_version=manifest_version,
        manifest_digest=raw_digest,
    ))


@csrf_exempt
@require_GET
def status(request):
    if not _management_authorized(request):
        return _management_denied()

    from base.services.sync.service import SyncService
    from base.services.sync.config import SyncConfig

    if not SyncConfig.is_enabled():
        return JsonResponse({'enabled': False, 'message': 'Sync not enabled'})

    return JsonResponse(SyncService.get_status())


@csrf_exempt
@require_POST
def trigger(request):
    if not _management_authorized(request):
        return _management_denied()

    from base.services.sync.service import SyncService
    from base.services.sync.config import SyncConfig, is_local_mode

    if not SyncConfig.is_enabled():
        return JsonResponse({'success': False, 'message': 'Sync not enabled'}, status=400)

    if not is_local_mode():
        return JsonResponse({'success': False, 'message': 'Only available in local mode'}, status=400)

    result = SyncService.push()
    return JsonResponse(result)


@csrf_exempt
@require_POST
def full_push(request):
    if not _management_authorized(request):
        return _management_denied()

    from base.services.sync.service import SyncService
    from base.services.sync.config import SyncConfig, is_local_mode

    if not SyncConfig.is_enabled():
        return JsonResponse({'success': False, 'message': 'Sync not enabled'}, status=400)

    if not is_local_mode():
        return JsonResponse({'success': False, 'message': 'Only available in local mode'}, status=400)

    result = SyncService.full_push()
    return JsonResponse(result)


@csrf_exempt
@require_GET
def queue_view(request):
    if not _management_authorized(request):
        return _management_denied()

    from base.services.sync.queue import SyncQueue

    records = SyncQueue.get_all()
    return JsonResponse({
        'count': len(records),
        'records': [{
            'model': r['model_name'],
            'uuid': r['uuid'],
            'created_at': r.get('created_at'),
            'attempts': r.get('attempts', 0),
            'last_error': r.get('last_error'),
        } for r in records[:100]],
    })


@csrf_exempt
@require_http_methods(["DELETE"])
def queue_clear(request):
    if not _management_authorized(request):
        return _management_denied()

    confirm = request.GET.get('confirm', '').lower() == 'true'
    if not confirm:
        return JsonResponse({
            'error': 'Add ?confirm=true to clear queue',
        }, status=400)

    from base.services.sync.queue import SyncQueue
    cleared = SyncQueue.clear()
    return JsonResponse({
        'success': True,
        'cleared': cleared,
        'message': (
            f'Cleared {cleared} rebuildable queue record(s); '
            'hard-delete tombstones were preserved'
        ),
    })


@csrf_exempt
@require_GET
def report(request):
    if not _management_authorized(request):
        return _management_denied()

    from base.services.sync.service import SyncService
    return JsonResponse(SyncService.status_report())


@csrf_exempt
@require_GET
def changes(request):
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if not auth.startswith('Branch '):
        return JsonResponse({'error': 'Invalid authorization'}, status=401)

    token = auth[7:]
    bound_branch, ok = _resolve_branch_token(token)
    if not ok:
        return JsonResponse({'error': 'Invalid branch token'}, status=401)

    from base.services.sync.config import SYNC_ORDER, get_all_models
    from django.utils.dateparse import parse_datetime

    requesting_branch = request.META.get('HTTP_X_BRANCH_ID', '')
    if bound_branch is not None:
        # A bound token may only claim its bound branch. Reject any mismatch
        # (including the catch-all 'unknown') so a token holder can't request
        # another branch's change feed. An absent/empty header is tolerated and
        # pinned to the bound branch (the response is scoped to it regardless).
        if requesting_branch and requesting_branch != bound_branch:
            return JsonResponse(
                {'error': f'X-Branch-ID does not match token (expected {bound_branch})'},
                status=403,
            )
        requesting_branch = bound_branch
    else:
        # Legacy unbound tokens have no token->branch identity. Apply the same
        # allowlist/fail-closed policy as the receive endpoint; otherwise one
        # leaked legacy token can choose any X-Branch-ID and read that branch's
        # full transactional feed. DEBUG retains the explicit development-only
        # compatibility path.
        allowed_ids = getattr(settings, 'ALLOWED_BRANCH_IDS', None)
        if allowed_ids:
            if requesting_branch not in allowed_ids:
                return JsonResponse(
                    {'error': 'X-Branch-ID is not in ALLOWED_BRANCH_IDS'},
                    status=403,
                )
        elif not settings.DEBUG:
            return JsonResponse(
                {'error': 'Unbound branch tokens are not permitted in production; '
                          'configure BRANCH_TOKEN_MAP or ALLOWED_BRANCH_IDS'},
                status=403,
            )

    # Heartbeat presence on the pull path too, so an idle till (nothing to push)
    # still refreshes its liveness every sync cycle. Best-effort.
    from base.services.presence import mark_device_live
    mark_device_live(
        request.META.get('HTTP_X_DEVICE_ID', ''),
        requesting_branch,
        request.META.get('HTTP_X_ACTIVE_CASHIER', ''),
    )

    since_param = request.GET.get('since')
    since_dt = parse_datetime(since_param) if since_param else None
    try:
        per_page = min(max(1, safe_per_page(request, 1000)), 5000)
    except (TypeError, ValueError):
        per_page = 1000

    # Freeze the high-water mark *before* reading any model.  Capturing it after
    # the per-model queries creates a lost-update window: a row committed after
    # its model was read but before the response timestamp would be absent from
    # this response yet older than (or equal to) the cursor the client stores.
    # The next pull would then skip it forever.  The terminal cursor is kept
    # one database-precision unit behind the cutoff below: an on_commit
    # publisher can receive the exact same microsecond as ``snapshot_cutoff``,
    # and a strict ``> since`` cursor must replay that boundary to remain safe.
    from django.utils import timezone
    snapshot_cutoff = timezone.now()

    models = get_all_models()
    data = {}
    total_records = 0
    has_more = False
    # The cursor we tell the client to resume from. With per-model paging we
    # can only safely advance to the *least* complete model's frontier — i.e.
    # the smallest "max synced_at returned" among the models that overflowed.
    # Advancing past that would skip another model's still-pending rows.
    next_since = None

    for name in SYNC_ORDER:
        model_class = models.get(name)
        if not model_class:
            continue
        if getattr(model_class, '_sync_pull_disabled', False):
            # One-way branch -> cloud evidence (AuditLog). Never expose it to
            # branch feeds, even when it belongs to a different branch.
            continue

        base_qs = model_class.objects.all()
        # Scope before the page cap. Transactional/history/command rows are
        # branch-owned: deliver only to their target branch (an own-push echo
        # is harmless and idempotent), never to peer branches. Shared catalog
        # and configuration models opt into global delivery explicitly.
        pull_scope = getattr(model_class, 'SYNC_PULL_SCOPE', 'branch')
        if pull_scope == 'disabled':
            continue
        if pull_scope == 'branch':
            if not requesting_branch:
                # An unbound/catch-all token has no safe transactional target.
                continue
            base_qs = base_qs.filter(branch_id=requesting_branch)
        elif pull_scope != 'global':
            logger.error(
                'Unknown SYNC_PULL_SCOPE=%r on %s; refusing feed exposure',
                pull_scope, model_class.__name__,
            )
            continue

        # NULL is the crash-safe, not-yet-published state. Promote a bounded
        # slice into the ordinary timestamp feed instead of materializing an
        # unbounded legacy NULL population in one response. Any remainder is
        # promoted on a later pull with a timestamp newer than this response's
        # cursor, so it cannot be skipped.
        null_pks = list(
            base_qs.filter(synced_at__isnull=True)
            .order_by('pk')
            .values_list('pk', flat=True)[:per_page]
        )
        null_fallback = []
        if null_pks:
            try:
                model_class.objects.filter(
                    pk__in=null_pks, synced_at__isnull=True,
                ).update(synced_at=snapshot_cutoff)
            except Exception:
                # A NULL row is the crash-safe lane precisely because the
                # after-commit publisher may have failed. If the bounded
                # promotion write is also transiently unavailable, still serve
                # only that selected slice directly. It remains NULL and will
                # replay until a later promotion succeeds: duplicates are safe,
                # dropping the committed change is not.
                logger.warning(
                    'sync changes: failed to promote bounded NULL slice for %s',
                    model_class.__name__,
                    exc_info=True,
                )
                null_fallback = list(
                    base_qs.filter(
                        pk__in=null_pks, synced_at__isnull=True,
                    ).order_by('pk')[:per_page]
                )

        # Only timestamped rows participate in the cursor frontier. Their
        # publication timestamp is assigned after commit. Anything published
        # beyond the cutoff belongs to the next pull; an equal-timestamp race is
        # covered by the one-microsecond terminal-cursor overlap below.
        timed_qs = base_qs.filter(
            synced_at__isnull=False,
            synced_at__lte=snapshot_cutoff,
        )
        if since_dt:
            timed_qs = timed_qs.filter(synced_at__gt=since_dt)
        timed_qs = timed_qs.order_by('synced_at', 'pk')

        timed_window = list(timed_qs[:per_page + 1])
        if len(timed_window) > per_page:
            has_more = True
            frontier = timed_window[per_page - 1].synced_at
            # Re-fetch the whole page up to AND INCLUDING the full frontier
            # timestamp group. The naive `window[:per_page]` can split a set of
            # rows that share one exact timestamp; a subsequent strict `>`
            # cursor would then skip its siblings forever.
            timed_window = list(timed_qs.filter(synced_at__lte=frontier))
            if next_since is None or frontier < next_since:
                next_since = frontier

        # When promotion failed, do not add a second timed page to the fallback
        # slice. This keeps the emergency lane bounded to per_page.
        window = null_fallback or timed_window

        records = [obj.to_sync_dict() for obj in window]
        if records:
            data[name] = records
            total_records += len(records)

    # PostgreSQL and Django datetimes have microsecond precision. Returning the
    # cutoff itself would assume a later publication is strictly newer, which
    # wall-clock precision cannot guarantee. Replay the boundary microsecond;
    # receiving an idempotent duplicate is safe, losing a change is not.
    from datetime import timedelta
    resume_cutoff = snapshot_cutoff - timedelta(microseconds=1)

    return JsonResponse({
        'success': True,
        'data': data,
        'total_records': total_records,
        'has_more': has_more,
        'next_since': next_since.isoformat() if next_since else None,
        'server_timestamp': resume_cutoff.isoformat(),
    })


@csrf_exempt
@require_POST
def trigger_pull(request):
    if not _management_authorized(request):
        return _management_denied()

    from base.services.sync.service import SyncService
    from base.services.sync.config import SyncConfig, is_local_mode

    if not SyncConfig.is_enabled():
        return JsonResponse({'success': False, 'message': 'Sync not enabled'}, status=400)

    if not is_local_mode():
        return JsonResponse({'success': False, 'message': 'Only available in local mode'}, status=400)

    result = SyncService.pull_from_cloud()
    return JsonResponse(result)


def get_sync_urls():
    from django.urls import path
    return [
        path('health', health, name='sync-health'),
        path('receive', receive, name='sync-receive'),
        path('shift-close/ack', shift_close_ack, name='sync-shift-close-ack'),
        path('status', status, name='sync-status'),
        path('trigger', trigger, name='sync-trigger'),
        path('trigger-pull', trigger_pull, name='sync-trigger-pull'),
        path('full-push', full_push, name='sync-full-push'),
        path('changes', changes, name='sync-changes'),
        path('queue', queue_view, name='sync-queue'),
        path('queue/clear', queue_clear, name='sync-queue-clear'),
        path('report', report, name='sync-report'),
    ]
