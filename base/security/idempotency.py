import json
import logging
from functools import wraps

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone

from base.models import IdempotencyKey

logger = logging.getLogger(__name__)

_SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}

INFLIGHT_TTL_SECONDS = 90


def _try_take_over_stale_claim(record):
    claimed_at = timezone.now()
    updated = IdempotencyKey.objects.filter(
        pk=record.pk,
        response_status=0,
        created_at=record.created_at,
    ).update(
        response_status=0,
        response_body={},
        created_at=claimed_at,
    )
    if updated:
        record.created_at = claimed_at
        record.response_status = 0
        record.response_body = {}
    return bool(updated)


def idempotent(scope):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if request.method in _SAFE_METHODS:
                return view_func(request, *args, **kwargs)

            key = (request.META.get('HTTP_IDEMPOTENCY_KEY') or '').strip()
            if not key or len(key) > 128:
                return view_func(request, *args, **kwargs)

            actor = getattr(request, 'user', None)
            actor_id = getattr(actor, 'id', None)
            if not actor_id:
                return view_func(request, *args, **kwargs)
            full_scope = f"{view_func.__module__}:{actor_id}:{scope}"
            record = None
            we_own_it = False
            try:
                with transaction.atomic():
                    record = IdempotencyKey.objects.create(
                        scope=full_scope,
                        key=key,
                        response_status=0,
                        response_body={},
                    )
                we_own_it = True
            except IntegrityError:
                record = IdempotencyKey.objects.filter(
                    scope=full_scope, key=key,
                ).first()

            if not we_own_it:
                if not record:
                    return view_func(request, *args, **kwargs)
                if record.response_status == 0:
                    age = (timezone.now() - record.created_at).total_seconds()
                    if age < INFLIGHT_TTL_SECONDS:
                        return JsonResponse(
                            {
                                'success': False,
                                'message': 'Duplicate request — original is still in progress.',
                            },
                            status=409,
                        )
                    we_own_it = _try_take_over_stale_claim(record)
                    if not we_own_it:
                        current = IdempotencyKey.objects.filter(pk=record.pk).first()
                        if current and current.response_status:
                            return JsonResponse(
                                current.response_body,
                                status=current.response_status,
                            )
                        return JsonResponse(
                            {
                                'success': False,
                                'message': 'Duplicate request — original is still in progress.',
                            },
                            status=409,
                        )
                else:
                    return JsonResponse(
                        record.response_body,
                        status=record.response_status,
                    )
            try:
                response = view_func(request, *args, **kwargs)
            except Exception:
                try:
                    IdempotencyKey.objects.filter(pk=record.pk).delete()
                except Exception:
                    logger.exception(
                        'failed to drop idempotency claim after view exception '
                        '(scope=%s key=%s)',
                        full_scope, key,
                    )
                raise

            content_type = (response.get('Content-Type') or '').lower()
            is_streaming = getattr(response, 'streaming', False)
            is_json = 'application/json' in content_type
            if is_streaming or not is_json:
                try:
                    IdempotencyKey.objects.filter(pk=record.pk).delete()
                except Exception:
                    logger.exception(
                        'failed to drop idempotency claim for non-cacheable response '
                        '(scope=%s key=%s ctype=%s)',
                        full_scope, key, content_type,
                    )
                return response

            body = {}
            try:
                if response.content:
                    body = json.loads(response.content)
            except (ValueError, TypeError):
                body = {}

            try:
                IdempotencyKey.objects.filter(pk=record.pk).update(
                    response_status=response.status_code,
                    response_body=body,
                )
            except Exception:
                logger.exception(
                    'failed to persist idempotency response (scope=%s key=%s)',
                    full_scope, key,
                )

            return response

        return wrapper

    return decorator
