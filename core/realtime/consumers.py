"""Channels consumers for the in-store realtime layer.

Sync consumers (run in a threadpool, so they can touch the ORM if needed).
Producers broadcast via ``core.realtime.publish`` using ``group_send`` with
``type='broadcast'``, which Channels routes to the ``broadcast()`` handler here.

Auth: the LAN appliance (desktop edition, OPEN_LAN) trusts its network and accepts
LAN clients with no token. The cloud server (OPEN_LAN off) REQUIRES a valid staff
session token on the handshake — otherwise these sockets would stream live order
data to any anonymous internet client. The licensing kill-switch is enforced here
too, because HTTP middleware does not run for the 'websocket' protocol.
"""
from urllib.parse import parse_qs

from asgiref.sync import async_to_sync
from channels.generic.websocket import JsonWebsocketConsumer
from django.conf import settings

# Group names. Single-branch (one till) for now; multi-branch can suffix BRANCH_ID.
ORDERS_GROUP = 'orders'
KDS_GROUP = 'kds'
CASHIERS_GROUP = 'cashiers'

_CLOSE_AUTH = 4401      # missing/invalid session
_CLOSE_FORBIDDEN = 4403  # license blocked / insufficient role


class _GroupConsumer(JsonWebsocketConsumer):
    group = None
    elevated = False     # subclasses set True to require an ADMIN/MANAGER session

    def connect(self):
        # 1) License kill-switch — also applies to websockets (the HTTP middleware
        #    never runs for the 'websocket' protocol). Mirrors the middleware's
        #    DEBUG-gated dev bypass so tests / dev are unaffected.
        if not getattr(settings, 'LICENSE_DEV_BYPASS', False):
            try:
                from licensing.services.state import get_state
                if get_state().is_blocked():
                    self.close(code=_CLOSE_FORBIDDEN)
                    return
            except Exception:
                pass  # never let a licensing hiccup wedge the LAN till

        # 2) Auth. OPEN_LAN (the trusted-LAN desktop edition) accepts LAN clients;
        #    the cloud server requires a valid staff session from the handshake.
        if not getattr(settings, 'OPEN_LAN', False):
            user = self._session_user()
            if user is None:
                self.close(code=_CLOSE_AUTH)
                return
            if self.elevated and getattr(user, 'role', None) not in ('ADMIN', 'MANAGER'):
                self.close(code=_CLOSE_FORBIDDEN)
                return

        if self.group:
            async_to_sync(self.channel_layer.group_add)(self.group, self.channel_name)
        self.accept()
        self.send_json({'type': 'connected', 'group': self.group})

    def disconnect(self, code):
        if self.group:
            async_to_sync(self.channel_layer.group_discard)(self.group, self.channel_name)

    def broadcast(self, event):
        # event == {'type': 'broadcast', 'payload': {...}} from publish._send
        self.send_json(event['payload'])

    # --- handshake auth helpers -------------------------------------------- #
    def _session_user(self):
        """Resolve a staff user from the handshake token, or None. Browsers can't
        set WS headers, so ?token=<session> is the norm; Authorization: Bearer is
        also accepted for non-browser clients."""
        token = self._handshake_token()
        if not token:
            return None
        try:
            from base.repositories import SessionRepository
            session = SessionRepository.get_by_session_key(token)
        except Exception:
            return None
        if not session or not session.user_id or session.user_id.is_deleted:
            return None
        if getattr(session.user_id, 'status', 'ACTIVE') != 'ACTIVE':
            return None
        return session.user_id

    def _handshake_token(self):
        qs = parse_qs((self.scope.get('query_string') or b'').decode('utf-8', 'ignore'))
        if qs.get('token'):
            return qs['token'][0]
        for key, val in (self.scope.get('headers') or []):
            if key == b'authorization' and val.startswith(b'Bearer '):
                return val[7:].decode('utf-8', 'ignore')
        return None


class OrderQueueConsumer(_GroupConsumer):
    """Live order feed for the cashier / customer display."""
    group = ORDERS_GROUP


class KdsConsumer(_GroupConsumer):
    """Kitchen Display System feed."""
    group = KDS_GROUP


class CashierControlConsumer(_GroupConsumer):
    """Server -> till control channel (lock cashier / force logout) — elevated."""
    group = CASHIERS_GROUP
    elevated = True
