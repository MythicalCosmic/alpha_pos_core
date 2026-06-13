from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.conf import settings
from base.helpers.request import safe_per_page


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
        # Require an explicit model_name — defaulting to 'order' would write a
        # malformed array as Orders.
        model_name = data[0].get('model_name')
        if not model_name:
            return JsonResponse(
                {'error': 'Array format requires model_name on the first item'},
                status=400,
            )
        records = [item.get('data', item) for item in data]
    elif isinstance(data, dict):
        model_name = data.get('model')
        records = data.get('records', [])
    else:
        return JsonResponse({'error': 'Expected JSON object or array'}, status=400)

    if not model_name or not records:
        return JsonResponse({'error': 'Missing model or records'}, status=400)

    from base.services.sync.receiver import CloudReceiver
    result = CloudReceiver.receive_batch(model_name, branch_id, records)

    return JsonResponse(result)


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
    SyncQueue.clear()
    return JsonResponse({'success': True, 'message': 'Queue cleared'})


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
    since_param = request.GET.get('since')
    since_dt = parse_datetime(since_param) if since_param else None
    try:
        per_page = min(max(1, safe_per_page(request, 1000)), 5000)
    except (TypeError, ValueError):
        per_page = 1000

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

        qs = model_class.objects.all()
        if since_dt:
            qs = qs.filter(synced_at__gt=since_dt)
        # Exclude the requester's own records in SQL, *before* the page cap.
        # Filtering after slicing would shrink a page below per_page and make
        # has_more / the frontier inconsistent with what was actually sent.
        if requesting_branch:
            qs = qs.exclude(branch_id=requesting_branch)
        # Order by synced_at so the page boundary is a well-defined frontier
        # the client can resume from. Cap at per_page+1 so a long-disconnected
        # branch cannot OOM the server in a single response.
        qs = qs.order_by('synced_at')

        window = list(qs[:per_page + 1])
        if len(window) > per_page:
            has_more = True
            frontier = window[per_page - 1].synced_at
            if frontier is not None:
                # Re-fetch the whole page up to AND INCLUDING the full frontier
                # timestamp group. The naive `window[:per_page]` can split a set
                # of rows that share one exact `synced_at` across the page
                # boundary; the client then resumes with the strict
                # `synced_at__gt=frontier` filter and the siblings left past the
                # cap are skipped forever (silent permanent loss). Bounded:
                # rows strictly before the frontier are < per_page, and a single
                # timestamp group is tiny in practice.
                window = list(qs.filter(synced_at__lte=frontier))
                if next_since is None or frontier < next_since:
                    next_since = frontier
            else:
                window = window[:per_page]

        records = [obj.to_sync_dict() for obj in window]
        if records:
            data[name] = records
            total_records += len(records)

    from django.utils import timezone
    return JsonResponse({
        'success': True,
        'data': data,
        'total_records': total_records,
        'has_more': has_more,
        'next_since': next_since.isoformat() if next_since else None,
        'server_timestamp': timezone.now().isoformat(),
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
        path('status', status, name='sync-status'),
        path('trigger', trigger, name='sync-trigger'),
        path('trigger-pull', trigger_pull, name='sync-trigger-pull'),
        path('full-push', full_push, name='sync-full-push'),
        path('changes', changes, name='sync-changes'),
        path('queue', queue_view, name='sync-queue'),
        path('queue/clear', queue_clear, name='sync-queue-clear'),
        path('report', report, name='sync-report'),
    ]
