import hashlib
import json
import logging
from functools import wraps
from uuid import UUID, uuid5

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

# Stable namespace for request action identifiers. A payment retry must be able
# to reconstruct the same business action after a worker commits the sale but
# dies before it caches the HTTP response. Never change this after release.
_ACTION_ID_NAMESPACE = UUID('2ef9bc68-2b34-5db1-92e0-fcad5fcf2f7e')


def _in_progress_response(retry_after_seconds=None):
    response = JsonResponse(
        {
            'success': False,
            'message': 'Duplicate request - original is still in progress.',
        },
        status=409,
    )
    if retry_after_seconds is not None:
        response['Retry-After'] = str(max(1, int(retry_after_seconds)))
    return response


def _try_take_over_stale_claim(record):
    """Atomically take ownership of the exact stale claim we observed.

    Two retries can read the same expired row before either updates it. A plain
    ``filter(pk=...).update(...)`` lets both retries believe they own the claim
    and execute the protected write twice. Matching the observed ``created_at``
    turns the takeover into a compare-and-swap: the first retry refreshes the
    lease and every other stale snapshot loses.
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


def idempotent(
    scope,
    *,
    fallback_key_from_request=False,
    expose_action_id=False,
    recover_inflight_after_seconds=None,
):
    """Deduplicate retries of a write endpoint.

    By default the contract remains opt-in through ``Idempotency-Key``:

    * The first (actor, resource, scope, key) executes and caches its response.
    * A completed exact retry replays the response without executing the view.
    * A changed request under the same key is rejected.
    * An in-flight retry receives 409 instead of double-acting.

    Money-critical, single-resource actions can enable
    ``fallback_key_from_request``. It derives a stable key from the exact
    actor/path/body, protecting deployed clients that do not yet send a header
    without making identical create requests globally idempotent.

    ``expose_action_id`` attaches a deterministic UUID to
    ``request.idempotency_action_id``. The service should persist that UUID
    with its business event and return stable success when the same action is
    retried. This recovers the database-commit / HTTP-cache crash window.

    Once a service implements that same-action recovery,
    ``recover_inflight_after_seconds`` may allow an exact retry to re-enter the
    view after a grace period. Until then the 409 includes ``Retry-After``.
    Never enable this for a write that is not idempotent by the exposed UUID.
    """

    if recover_inflight_after_seconds is not None:
        if not expose_action_id:
            raise ValueError(
                'recover_inflight_after_seconds requires expose_action_id=True'
            )
        if recover_inflight_after_seconds < 0:
            raise ValueError(
                'recover_inflight_after_seconds must be zero or greater'
            )

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if request.method in _SAFE_METHODS:
                return view_func(request, *args, **kwargs)

            supplied_key = (
                request.META.get('HTTP_IDEMPOTENCY_KEY') or ''
            ).strip()
            if len(supplied_key) > 128:
                # Silently bypassing protection for an invalid client key is
                # dangerous: the caller believes it supplied a retry identity
                # while the server would execute every retry as new.
                return JsonResponse(
                    {
                        'success': False,
                        'message': (
                            'Idempotency-Key must be at most 128 characters.'
                        ),
                    },
                    status=400,
                )
            if not supplied_key and not fallback_key_from_request:
                return view_func(request, *args, **kwargs)

            actor = getattr(request, 'user', None)
            actor_id = getattr(actor, 'id', None)
            if not actor_id:
                # No identified user means there is no safe per-user namespace.
                # Sharing actor_id=0 could replay one anonymous caller's
                # response to another.
                return view_func(request, *args, **kwargs)

            # Include the concrete resource path. Without it, reusing one key
            # for order A and order B could replay A's cached payment success
            # without ever executing B's service.
            resource = f'{request.method}:{request.path_info}'
            resource_hash = hashlib.sha256(
                resource.encode('utf-8')
            ).hexdigest()[:16]
            full_scope = (
                f'{view_func.__module__}:{actor_id}:{scope}:{resource_hash}'
            )
            fingerprint_input = b'\x00'.join((
                request.method.encode('utf-8'),
                request.path_info.encode('utf-8'),
                (request.META.get('QUERY_STRING') or '').encode('utf-8'),
                request.body or b'',
            ))
            request_fingerprint = hashlib.sha256(
                fingerprint_input
            ).hexdigest()
            key_was_generated = not bool(supplied_key)
            key = supplied_key or f'auto:{request_fingerprint}'

            if expose_action_id:
                action_name = '\x00'.join((
                    full_scope,
                    key,
                    request_fingerprint,
                ))
                request.idempotency_action_id = uuid5(
                    _ACTION_ID_NAMESPACE,
                    action_name,
                )
                request.idempotency_key_was_generated = key_was_generated

            # Claim the row in its own short transaction. This makes the claim
            # visible before a potentially longer business transaction.
            record = None
            owns_claim = False
            try:
                with transaction.atomic():
                    record = IdempotencyKey.objects.create(
                        scope=full_scope,
                        key=key,
                        request_fingerprint=request_fingerprint,
                        response_status=0,
                        response_body={},
                    )
                owns_claim = True
            except IntegrityError:
                record = IdempotencyKey.objects.filter(
                    scope=full_scope,
                    key=key,
                ).first()

            if not owns_claim:
                if not record:
                    # A competing exception may have removed the row between
                    # our unique-constraint loss and SELECT. Running without a
                    # claim would defeat the payment guarantee; a short retry
                    # is safer and will create a fresh claim.
                    return _in_progress_response(1)

                if (
                    record.request_fingerprint
                    and record.request_fingerprint != request_fingerprint
                ):
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

                if record.response_status:
                    return JsonResponse(
                        record.response_body,
                        status=record.response_status,
                    )

                age = (timezone.now() - record.created_at).total_seconds()
                if age < INFLIGHT_TTL_SECONDS:
                    if (
                        recover_inflight_after_seconds is None
                        or age < recover_inflight_after_seconds
                    ):
                        retry_after = None
                        if recover_inflight_after_seconds is not None:
                            retry_after = (
                                recover_inflight_after_seconds - age
                            )
                        return _in_progress_response(retry_after)

                    # This exact request may safely re-enter only because the
                    # endpoint opted into a deterministic business action ID.
                    # We do not own the claim; exception cleanup must not
                    # remove it while the original worker may still be alive.
                    pass
                else:
                    # Stale zombie claim. Take over only the exact lease
                    # timestamp observed so concurrent retries cannot both win.
                    owns_claim = _try_take_over_stale_claim(record)
                    if not owns_claim:
                        current = IdempotencyKey.objects.filter(
                            pk=record.pk
                        ).first()
                        if current and current.response_status:
                            return JsonResponse(
                                current.response_body,
                                status=current.response_status,
                            )
                        return _in_progress_response(1)

            # Run the view and persist its response. If an owning execution
            # raises, remove only a still-empty claim so a retry can start. A
            # concurrent recovery may already have cached the success.
            try:
                response = view_func(request, *args, **kwargs)
            except Exception:
                if owns_claim:
                    try:
                        IdempotencyKey.objects.filter(
                            pk=record.pk,
                            response_status=0,
                        ).delete()
                    except Exception:
                        logger.exception(
                            'failed to drop idempotency claim after view '
                            'exception (scope=%s key=%s)',
                            full_scope,
                            key,
                        )
                raise

            # Streaming/file responses cannot be replayed safely. An owning
            # execution drops its empty claim; a speculative recovery leaves
            # the original worker's claim untouched.
            content_type = (response.get('Content-Type') or '').lower()
            is_streaming = getattr(response, 'streaming', False)
            is_json = 'application/json' in content_type
            if is_streaming or not is_json:
                if owns_claim:
                    try:
                        IdempotencyKey.objects.filter(
                            pk=record.pk,
                            response_status=0,
                        ).delete()
                    except Exception:
                        logger.exception(
                            'failed to drop idempotency claim for '
                            'non-cacheable response '
                            '(scope=%s key=%s ctype=%s)',
                            full_scope,
                            key,
                            content_type,
                        )
                return response

            body = {}
            try:
                if response.content:
                    body = json.loads(response.content)
            except (ValueError, TypeError):
                body = {}

            # A fallback key exists to protect old clients, not to make a
            # precondition/validation failure permanent. For example, a
            # cashier may open the required shift and submit the exact same
            # checkout again. Explicit client keys retain the traditional
            # cache-all-responses contract; generated keys are retained only
            # after a successful business action.
            is_success = 200 <= response.status_code < 300
            if owns_claim and key_was_generated and not is_success:
                try:
                    IdempotencyKey.objects.filter(
                        pk=record.pk,
                        response_status=0,
                    ).delete()
                except Exception:
                    logger.exception(
                        'failed to drop generated idempotency claim after '
                        'unsuccessful response (scope=%s key=%s)',
                        full_scope,
                        key,
                    )

            # A speculative same-action replay must not overwrite an eventual
            # original success with a transient error. Only its successful
            # recovery response is authoritative.
            should_cache = (
                is_success
                or (owns_claim and not key_was_generated)
            )
            if should_cache:
                try:
                    IdempotencyKey.objects.filter(pk=record.pk).update(
                        response_status=response.status_code,
                        response_body=body,
                    )
                except Exception:
                    logger.exception(
                        'failed to persist idempotency response '
                        '(scope=%s key=%s)',
                        full_scope,
                        key,
                    )

            return response

        return wrapper

    return decorator
