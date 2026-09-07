"""Commit the command and its exact replay response in one transaction."""

import hashlib
from uuid import UUID, uuid5

from django.db import transaction

from base.helpers.response import ServiceResponse
from base.models import IdempotencyKey
from . import json_numbers

NAMESPACE = UUID("5950b201-b5d9-4328-8808-c5b11604e854")


class InvoiceError(Exception):
    def __init__(self, result):
        self.result = result
        super().__init__(result[0]["message"])


def fail(code, message, status=422, *, field=None, details=None):
    raise InvoiceError(
        ServiceResponse.failure(
            code,
            message,
            status,
            errors={field: [message]} if field else None,
            details=details,
        )
    )


def execute(*, actor, branch, operation, target, key, payload, command):
    if not isinstance(key, str) or not key.strip():
        return ServiceResponse.failure(
            "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required.", 422
        )
    key = key.strip()
    if len(key) > 128:
        return ServiceResponse.failure(
            "IDEMPOTENCY_KEY_INVALID", "Idempotency-Key is too long.", 422
        )
    scope = f"invoice:actor:{actor.id}:branch:{branch}:operation:{operation}:target:{target}"
    fingerprint = hashlib.sha256(json_numbers.dumps(payload).encode()).hexdigest()
    action = uuid5(NAMESPACE, scope + "\0" + key)
    try:
        with transaction.atomic():
            # A competing INSERT waits for the first transaction to commit.
            # A failed command rolls the claim back with every business write.
            record, _ = IdempotencyKey.objects.get_or_create(
                scope=scope,
                key=key,
                defaults={"request_fingerprint": fingerprint},
            )
            record = IdempotencyKey.objects.select_for_update().get(pk=record.pk)
            if record.request_fingerprint != fingerprint:
                fail(
                    "IDEMPOTENCY_KEY_REUSED",
                    "This key already identifies different invoice data.",
                    409,
                )
            if record.response_status:
                from .serialization import protect_replay

                body = json_numbers.loads(record.response_body["canonical_json"])
                return protect_replay(body, actor), record.response_status
            body, status = command(action, key)
            if status >= 400:
                raise InvoiceError((body, status))
            record.response_status = status
            record.response_body = {"canonical_json": json_numbers.dumps(body)}
            record.save(
                update_fields=["response_status", "response_body", "updated_at"]
            )
            return body, status
    except InvoiceError as exc:
        return exc.result
