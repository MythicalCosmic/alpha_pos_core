import json
import hashlib
import logging
from functools import wraps

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone

from base.models import IdempotencyKey

logger = logging.getLogger(__name__)

_SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}

# An in-flight claim (response_status == 0) older than this is treated as a
# zombie left behind by a crashed/killed worker. Without this, such a row would
# wedge every future retry into the 409 in-progress branch forever. Kept short:
# long enough to outlast a normal request, short enough that a real crash
# recovers quickly.
INFLIGHT_TTL_SECONDS = 90


def _try_take_over_stale_claim(record):
    """Atomically take ownership of the exact stale claim we observed.

    Two retries can read the same expired row before either updates it.  A
    plain ``filter(pk=...).update(...)`` lets both retries believe they own the
    claim and execute the protected write twice.  Matching the observed
    ``created_at`` turns the takeover into a compare-and-swap: the first retry
    refreshes the lease and every other stale snapshot loses.
    """
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
    """Dedup retries of a write endpoint by the `Idempotency-Key` header.

    Behavior when the header is present:
    * First time we see (actor, scope, key) → execute the view, store the
      response, return it.
    * The same key replayed after the original finished → return the stored
      response without re-executing.
    * The same key replayed while the original is still in flight → 409 so
      the second attempt doesn't double-act.

    Without the header the view runs unchanged — the contract change is
    opt-in for clients.
    """

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
                # No identified user → no per-user namespace. Sharing
                # actor_id=0 across every anonymous caller would let one
                # client's stored response replay for another. Better to
                # fall through and execute the view; idempotency is opt-in
                # for clients and only meaningful with a real actor.
                return view_func(request, *args, **kwargs)
            # Scope is qualified by the view module *and concrete resource
            # path*.  The latter is money-critical: without it, a POS client
            # that accidentally reuses one Idempotency-Key for order A and
            # order B receives order A's cached 200 for order B even though
            # order B's pay service never ran.  The UI can then print/confirm
            # payment while the Order remains unpaid and has no OrderPayment
            # rows.  Hash the method + path to keep the value comfortably
            # within IdempotencyKey.scope's 100-character column.
            resource = f'{request.method}:{request.path_info}'
            resource_hash = hashlib.sha256(resource.encode('utf-8')).hexdigest()[:16]
            full_scope = (
                f"{view_func.__module__}:{actor_id}:{scope}:{resource_hash}"
            )
            fingerprint_input = b'\x00'.join((
                request.method.encode('utf-8'),
                request.path_info.encode('utf-8'),
                (request.META.get('QUERY_STRING') or '').encode('utf-8'),
                request.body or b'',
            ))
            request_fingerprint = hashlib.sha256(fingerprint_input).hexdigest()

            # Claim the (scope, key) row. Losing the unique-constraint race
            # means another request already owns it — fall through to the
            # replay / in-flight branch.
            record = None
            we_own_it = False
            try:
                with transaction.atomic():
                    record = IdempotencyKey.objects.create(
                        scope=full_scope,
                        key=key,
                        request_fingerprint=request_fingerprint,
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
                    # Vanishingly unlikely race (row was deleted between the
                    # IntegrityError and the lookup). Behave like a fresh
                    # request rather than 500.
                    return view_func(request, *args, **kwargs)
                if (
                    record.request_fingerprint
                    and record.request_fingerprint != request_fingerprint
                ):
                    # One client key represents exactly one request. Returning
                    # the old success for a changed amount/tender is a false
                    # acknowledgement; executing it is a duplicate-money risk.
                    return JsonResponse(
                        {
                            'success': False,
                            'message': (
                                'Idempotency-Key was already used for a '
                                'different request.'
                            ),
                        },
                        status=409,
                    )
                if record.response_status == 0:
                    age = (timezone.now() - record.created_at).total_seconds()
                    if age < INFLIGHT_TTL_SECONDS:
                        # Genuinely still in flight — fail fast so the retry
                        # doesn't double-act.
                        return JsonResponse(
                            {
                                'success': False,
                                'message': 'Duplicate request — original is still in progress.',
                            },
                            status=409,
                        )
                    # Stale zombie claim from a crashed worker. Take over only
                    # the exact lease timestamp we observed. Another retry may
                    # have refreshed it between our SELECT and UPDATE.
                    we_own_it = _try_take_over_stale_claim(record)
                    if not we_own_it:
                        current = IdempotencyKey.objects.filter(pk=record.pk).first()
                        if current and current.response_status:
                            return JsonResponse(
                                current.response_body,
                                status=current.response_status,
                            )
                        # The winning retry is still running (or removed its
                        # claim after an exception). Never execute from this
                        # stale snapshot; the client can retry safely.
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

            # We won the claim. Run the view and persist its response.
            # If the view raises, drop the claim so a retry can run fresh —
            # otherwise the zombie row stays at response_status=0 forever
            # and every future retry hits the in-progress 409 branch above.
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

            # Streaming/file responses can't be cached: reading .content on a
            # StreamingHttpResponse exhausts the iterator, and replaying a
            # 200/empty body in place of the original file download would be
            # worse than just letting the client retry. Skip persistence and
            # delete the claim row so a retry can run fresh.
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
