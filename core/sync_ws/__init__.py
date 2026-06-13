"""Websocket sync transport — a TRANSPORT swap, not a rewrite.

Only ``base/services/sync/transport.py`` (blocking requests + sleep backoff) and
``base/services/sync/views.py`` (HTTP endpoints) are replaced here. The durable
outbox/queue, the pull cursor, the idempotent receiver, conflict resolution and the
FK-ordering passes in ``base/services/sync/{queue,cursor,receiver,service,config}.py``
are REUSED unchanged.

The WS frames MUST carry the same contracts the HTTP transport used, or paging /
partial-batch logic breaks:
  push ack : {success, created, updated, skipped, failed_uuids, errors}
  pull     : {data, server_timestamp, has_more, next_since}

The WS branch bypasses Django middleware, so the consumer's ``connect()`` must
re-assert the licensing kill-switch + the BRANCH_TOKEN_MAP / X-Branch-ID check that
``base/services/sync/views.py`` does today.

TODO(Phase 4): implement WsSyncTransport + SyncIngestConsumer; keep HTTP as fallback.
"""
