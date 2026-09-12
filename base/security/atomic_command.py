"""Persist an HTTP command and its replay response in the business transaction.

Unlike a short-lived claim followed by a separate commit, a worker cannot commit
the business event without its response. The unique key serializes concurrent
requests; a rollback removes both the event and its claim.
"""
import hashlib
import json
from functools import wraps

from django.db import transaction
from django.http import JsonResponse

from base.models import IdempotencyKey


class _RejectedCommand(Exception):
    def __init__(self, response):
        self.response = response


def _error(code, message, status=409):
    return JsonResponse({'success': False, 'code': code, 'message': message}, status=status)


def atomic_command(scope, *, actor_attribute='user', required=True):
    def decorate(view):
        @wraps(view)
        def execute(request, *args, **kwargs):
            key = (request.headers.get('Idempotency-Key') or '').strip()
            if not key:
                if not (required(request) if callable(required) else required):
                    return view(request, *args, **kwargs)
                return _error('IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key is required.', 422)
            if len(key) > 128:
                return _error('INVALID_IDEMPOTENCY_KEY', 'Idempotency-Key exceeds 128 characters.', 422)
            actor = getattr(request, actor_attribute, None)
            if actor is None or not actor.pk:
                return _error('AUTHENTICATION_REQUIRED', 'Authentication is required.', 401)
            try:
                payload = json.loads(request.body or b'{}')
                canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
            except (ValueError, TypeError, UnicodeDecodeError):
                return _error('INVALID_JSON', 'Provide valid finite JSON values.', 422)
            identity = ':'.join((actor._meta.label_lower, str(actor.pk),
                                 str(getattr(actor, 'branch_id', '') or ''), scope))
            fingerprint = hashlib.sha256('\0'.join((request.method, request.path_info,
                                                    canonical)).encode()).hexdigest()
            with transaction.atomic():
                record, created = IdempotencyKey.objects.get_or_create(
                    scope='atomic:' + hashlib.sha256(identity.encode()).hexdigest(),
                    key=key,
                    defaults={'request_fingerprint': fingerprint,
                              'response_status': 0, 'response_body': {}},
                )
                if not created:
                    if record.request_fingerprint != fingerprint:
                        return _error('IDEMPOTENCY_KEY_REUSED', 'This key belongs to a different request.')
                    if not record.response_status:
                        return _error('COMMAND_IN_PROGRESS', 'The original request is still in progress.')
                    return JsonResponse(record.response_body, status=record.response_status,
                                        safe=False, json_dumps_params={'sort_keys': True})
                try:
                    with transaction.atomic():
                        response = view(request, *args, **kwargs)
                        if response.status_code >= 400:
                            raise _RejectedCommand(response)
                except _RejectedCommand as rejected:
                    response = rejected.response
                if getattr(response, 'streaming', False):
                    raise TypeError('Atomic commands must return a JSON response')
                body = json.loads(response.content)
                record.response_status = response.status_code
                record.response_body = body
                record.save(update_fields=['response_status', 'response_body'])
                response.content = JsonResponse(body, safe=False,
                                               json_dumps_params={'sort_keys': True}).content
                return response
        return execute
    return decorate
